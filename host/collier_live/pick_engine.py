"""Heuristic gesture engine that replaces the TCN in the live hot path.

This rewrite leans on the original Collier's *proven* primitives instead of
inventing parallel ones:

* **Pick state** comes from ``collier.selection.PickLatch`` (already running
  inside the live conductor via ``SelectionResolver.resolve(...).arm.picked``).
  PickLatch uses the collier conditioner's *adaptive* flex envelope, which
  keeps following the current sensor range — so it stays accurate even
  when the user's bend baselines have drifted. The latch engages when any
  non-thumb finger crosses ``finger_open_max`` while ``hand_steady`` and
  releases when every non-thumb finger opens. This is the same primitive
  that prints "PICK" in the HUD and that the user trusts.
* **Spawn** is counted from the same pick latch: ``spawn_min_picks`` short
  picks (each held <= ``spawn_pick_max_s`` with no real vertical travel)
  within ``spawn_window_s`` fire ``spawn_note``. (An earlier draft of this
  docstring described the conditioner's ``spawn_flutter`` envelope; the code
  below does not use it.)

What this engine adds on top:
  * Integrate ``vert_vel`` while picked; on release decide ``scale_up`` /
    ``scale_down`` from net signed displacement + direction-dominance.
  * Collective: a scale release with ``|palm_roll| >= palm_roll_collective_deg``
    fires ``collective_scale_*`` (side "both") at once, then locks out the
    other hand for ``collective_cooldown_s``.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum


@dataclass
class PickEngineConfig:
    # ---- Scale: pick-pull-release motion ----------------------------
    # ``picked`` comes in as a boolean from the conductor (PickLatch state),
    # so there are no openness thresholds here — the latched-state edges
    # define the pick start and pick release.
    min_pick_s: float = 0.08
    max_pick_s: float = 2.50
    dy_min: float = 0.10
    direction_dominance: float = 0.50

    # Per-hand cooldown for single-hand scale fires. L and R cool down
    # independently so the user can land two notes within the same window —
    # which the previous engine-wide cooldown made impossible.
    scale_cooldown_s: float = 0.30

    # ---- Collective (palm-orientation gated) ------------------------
    # palm_roll is in DEGREES. When a single hand releases a scale gesture
    # with ``|palm_roll| >= palm_roll_collective_deg`` we treat the user's
    # intent as collective and fire ``collective_scale_*`` immediately
    # (one event, side="both"). No waiting for the other hand.
    palm_roll_collective_deg: float = 18.0
    # Global lock applied when ANY collective fires. The other hand is
    # almost certainly also rolled and about to release, and we don't want
    # its release to fire a second collective. Both hands' per-hand scale
    # cooldown is also set so the other hand can't emit a stray single-
    # hand fire either.
    collective_cooldown_s: float = 0.50

    # ---- Spawn note (rapid open->pick->open->pick wiggle) ----------
    # Spawn fires when we see ``spawn_min_picks`` short picks within
    # ``spawn_window_s`` where each pick's vertical displacement stayed
    # below ``dy_min`` (i.e. they weren't scale gestures) and each pick
    # was held no longer than ``spawn_pick_max_s`` (long picks are
    # intentional grabs, not wiggles). Per-hand refractory only —
    # spawn from L doesn't block R, and vice versa.
    spawn_min_picks: int = 3
    spawn_window_s: float = 1.0
    spawn_pick_max_s: float = 0.30
    spawn_refractory_s: float = 0.40


@dataclass
class HandTick:
    """One per-hand sample for the engine.

    All three signals come from the live pipeline already running in
    ``conductor._tick_loop``; no extra computation is needed here.
    """
    present: bool
    picked: bool      # sel.arm(side).picked from collier PickLatch
    vert_vel: float   # ml_sig.vert_vel (IMU-derived, drift-immune)
    palm_roll: float  # ml_sig.palm_roll (IMU-derived, in degrees)

    @classmethod
    def absent(cls) -> "HandTick":
        return cls(False, False, 0.0, 0.0)


@dataclass
class GestureEvent:
    label: str   # scale_up / scale_down / collective_scale_up / collective_scale_down / spawn_note
    side: str    # "L" / "R" / "both"
    t: float
    confidence: float = 1.0


@dataclass
class _HandState:
    picked: bool = False
    pick_t0: float = 0.0
    dy_accum: float = 0.0
    dy_peak: float = 0.0
    dy_trough: float = 0.0
    # Recent short non-scale picks → spawn-note wiggle detector.
    # Each entry is the timestamp of the pick release.
    quick_pick_times: deque = field(default_factory=lambda: deque(maxlen=12))
    spawn_refr_until: float = -1e9
    last_scale_fire_t: float = -1e9


class PickGestureEngine:
    def __init__(self, cfg: PickEngineConfig | None = None):
        self.cfg = cfg or PickEngineConfig()
        self._h = {"L": _HandState(), "R": _HandState()}
        # Global lock only for collective fires — prevents the second
        # hand's release from emitting a second collective right after.
        # Single-hand scale fires use per-hand cooldowns instead.
        self._collective_lock_until: float = -1e9

    def reset(self) -> None:
        self._h = {"L": _HandState(), "R": _HandState()}
        self._collective_lock_until = -1e9

    def update(self, *, t: float, dt: float, L: HandTick | None, R: HandTick | None) -> list[GestureEvent]:
        L = L or HandTick.absent()
        R = R or HandTick.absent()
        events: list[GestureEvent] = []
        for side, tick in (("L", L), ("R", R)):
            ev = self._step_pick(side, tick, t, dt)
            if ev is not None:
                events.append(ev)
        return events

    # ------------------------------------------------------------- pick

    def _step_pick(self, side: str, tick: HandTick, t: float, dt: float) -> GestureEvent | None:
        st = self._h[side]
        c = self.cfg
        if not tick.present:
            if st.picked:
                st.picked = False
            return None

        # ``picked`` is a latched bool from collier.selection.PickLatch.
        if not st.picked and tick.picked:
            st.picked = True
            st.pick_t0 = t
            st.dy_accum = 0.0
            st.dy_peak = 0.0
            st.dy_trough = 0.0
            return None

        if st.picked and tick.picked:
            st.dy_accum += tick.vert_vel * dt
            st.dy_peak = max(st.dy_peak, st.dy_accum)
            st.dy_trough = min(st.dy_trough, st.dy_accum)
            if (t - st.pick_t0) > c.max_pick_s:
                st.picked = False
            return None

        if st.picked and not tick.picked:
            # Pick release: classify as scale, spawn-eligible quick pick,
            # or noise. Scales fire immediately (no pending queue).
            held = t - st.pick_t0
            st.picked = False
            net = st.dy_accum
            span = max(1e-6, st.dy_peak - st.dy_trough)
            dom = abs(net) / span if span > 0 else 0.0

            is_scale = (
                held >= c.min_pick_s
                and abs(net) >= c.dy_min
                and dom >= c.direction_dominance
            )
            if is_scale:
                # A real scale just happened — clear this hand's wiggle
                # tracker so an alternating up/down sequence can't sneak
                # into a spawn.
                st.quick_pick_times.clear()
                direction = "up" if net > 0 else "down"
                rolled = abs(tick.palm_roll) >= c.palm_roll_collective_deg
                if rolled:
                    # Orientation says "collective". Block if another
                    # collective just fired (the other hand probably
                    # already covered this gesture).
                    if t < self._collective_lock_until:
                        return None
                    self._collective_lock_until = t + c.collective_cooldown_s
                    # Also lock both hands' single-hand path so the other
                    # hand's release can't fire a stray scale right after.
                    self._h["L"].last_scale_fire_t = t
                    self._h["R"].last_scale_fire_t = t
                    return GestureEvent(f"collective_scale_{direction}", "both", t)
                # Single-hand: per-hand cooldown only. L and R are now
                # independent — both hands can fire within the same window.
                if t - st.last_scale_fire_t < c.scale_cooldown_s:
                    return None
                st.last_scale_fire_t = t
                return GestureEvent(f"scale_{direction}", side, t)

            # Spawn-eligible quick pick (brief, no displacement). Per-hand
            # refractory — L's spawn doesn't block R's.
            if held <= c.spawn_pick_max_s:
                st.quick_pick_times.append(t)
                cutoff = t - c.spawn_window_s
                while st.quick_pick_times and st.quick_pick_times[0] < cutoff:
                    st.quick_pick_times.popleft()
                if (
                    len(st.quick_pick_times) >= c.spawn_min_picks
                    and t >= st.spawn_refr_until
                ):
                    st.quick_pick_times.clear()
                    st.spawn_refr_until = t + c.spawn_refractory_s
                    return GestureEvent("spawn_note", side, t)
            return None
        return None

    def snapshot(self) -> dict:
        return {
            side: {
                "picked": st.picked,
                "dy_accum": round(st.dy_accum, 3),
                "dy_peak": round(st.dy_peak, 3),
                "dy_trough": round(st.dy_trough, 3),
                "quick_picks": len(st.quick_pick_times),
            }
            for side, st in self._h.items()
        }
