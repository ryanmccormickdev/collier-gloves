"""Post-processing: debounce ML labels, refractory, map to GestureCommand."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from collier.selection import Selection

from .commands import GestureCommand
from .config import PostprocessConfig


@dataclass
class _HandState:
    history: deque[str] = field(default_factory=lambda: deque(maxlen=8))
    refractory_until: dict[str, float] = field(default_factory=dict)
    # Label currently "occupying" the hand. Stays set until the model has
    # produced enough low-confidence / none ticks to officially close the
    # episode (see GesturePostProcessor._episode_closed).
    episode_fired: str | None = None
    # Consecutive non-occupying ticks (used by the hysteresis close rule).
    release_ticks: int = 0


class GesturePostProcessor:
    """
    Turn per-hand window-level class labels into discrete commands.

    TCN output is ~1 s of motion, so the same class often holds for many ticks.
    Strategy:
      * Require ``agreement_k`` consecutive ticks with the same non-``none``
        label before considering a fire.
      * Apply per-class threshold + ``confidence_bump`` + ``start_margin``
        before firing.
      * After a fire, keep ``episode_fired`` set until either the model
        produces ``release_ticks_needed`` ticks of ``none`` *or* of a
        below-threshold prediction with conf < (threshold - end_margin).
        This prevents the release/return half of any gesture from being
        misinterpreted as a *new* gesture episode.

    Tuned against ``experiments/ml-attempt/apps/diag_offline_predict.py`` so
    single physical gestures in the training set fire exactly one command. See
    ``experiments/ml-attempt/docs/diagnosis_findings.md``.
    """

    # Number of consecutive ticks at "released" confidence before we let the
    # same physical hand emit a new episode.
    _RELEASE_TICKS_NEEDED = 6  # 100ms at 60 Hz

    def __init__(
        self,
        cfg: PostprocessConfig,
        n_regions: int,
        thresholds_by_side: dict[str, dict[str, float]] | None = None,
    ):
        self.cfg = cfg
        self.n_regions = n_regions
        self.thresholds_by_side = thresholds_by_side or {"L": {}, "R": {}}
        self._hands = {"L": _HandState(), "R": _HandState()}
        self._global_cooldown_until = 0.0
        self._region_hist: dict[str, list[int | None]] = {"L": [], "R": []}
        self.last_block_reason: str | None = None

    def reset(self) -> None:
        for h in self._hands.values():
            h.history.clear()
            h.refractory_until.clear()
            h.episode_fired = None
            h.release_ticks = 0
        self._global_cooldown_until = 0.0
        for k in self._region_hist:
            self._region_hist[k].clear()
        self.last_block_reason = None

    def _update_release(self, st: _HandState, label: str, confidence: float) -> None:
        """Increment / reset the per-hand release counter and close the episode
        once we've seen enough below-threshold ticks. This is what stops the
        return half of a gesture from being treated as a new gesture."""
        if st.episode_fired is None:
            st.release_ticks = 0
            return
        side_thr_map = {
            side: self.thresholds_by_side.get(side, {}) for side in ("L", "R")
        }
        thr_for_episode = max(
            side_thr_map.get(st_side, {}).get(st.episode_fired, 0.65)
            for st_side in ("L", "R")
            if self._hands[st_side] is st
        )
        end_thr = max(0.0, thr_for_episode - self.cfg.end_margin)
        released_tick = (
            label == "none"
            or label != st.episode_fired
            or confidence < end_thr
        )
        if released_tick:
            st.release_ticks += 1
            if st.release_ticks >= self._RELEASE_TICKS_NEEDED:
                st.episode_fired = None
                st.release_ticks = 0
        else:
            st.release_ticks = 0

    def note_region(self, side: str, region: int | None) -> None:
        hist = self._region_hist[side]
        hist.append(region)
        if len(hist) > self.cfg.region_lookback:
            del hist[0 : len(hist) - self.cfg.region_lookback]

    def process(
        self,
        side: str,
        label: str,
        confidence: float,
        sel: Selection,
    ) -> GestureCommand | None:
        self.last_block_reason = None
        st = self._hands[side]

        # Collective labels are routed through try_collective() but we still
        # need to count them toward release-hysteresis on this hand, so an
        # immediate single-hand label after a collective gesture has to wait
        # for the same release window.
        st.history.append(label)
        self._update_release(st, label, confidence)
        if label.startswith("collective_"):
            return None

        if label == "none":
            return None

        if not self._debounced(st, label):
            self.last_block_reason = f"{side}: debounce {label}"
            return None

        if st.episode_fired is not None:
            self.last_block_reason = (
                f"{side}: episode {st.episode_fired} still active"
            )
            return None

        now = time.monotonic()
        if now < self._global_cooldown_until:
            self.last_block_reason = "global cooldown"
            return None
        if label in st.refractory_until and now < st.refractory_until[label]:
            self.last_block_reason = f"{side}: refractory {label}"
            return None

        thr = self._threshold(side, label) + self.cfg.start_margin
        if confidence < thr:
            self.last_block_reason = f"{side}: conf {confidence:.2f} < start_thr {thr:.2f}"
            return None

        cmd = label_to_command(
            label,
            side,
            sel,
            self._region_hist,
            self.n_regions,
            self.cfg,
        )
        if cmd is None:
            self.last_block_reason = f"{side}: no region for {label}"
            return None

        st.episode_fired = label
        st.release_ticks = 0
        st.refractory_until[label] = now + self.cfg.per_class_refractory_s
        self._global_cooldown_until = now + self.cfg.global_cooldown_s
        return cmd

    def try_collective(
        self,
        label_l: str,
        label_r: str,
        conf_l: float,
        conf_r: float,
        sel: Selection,
    ) -> GestureCommand | None:
        """Collective gestures: prefer the "both sides agree" path. If
        ``collective_requires_both`` is True we ignore single-side collective
        predictions entirely (the more conservative — and recommended — default
        now). When both sides predict the same collective_* label we fire once
        from the L side and ride that side's episode/refractory state."""
        now = time.monotonic()
        if now < self._global_cooldown_until:
            return None

        if (
            label_l.startswith("collective_")
            and label_r.startswith("collective_")
            and label_l == label_r
        ):
            lab = label_l
            conf = 0.5 * (conf_l + conf_r)
            return self._fire_collective("L", lab, conf, sel, now)

        if self.cfg.collective_requires_both:
            return None

        for side, lab, conf in (
            ("L", label_l, conf_l),
            ("R", label_r, conf_r),
        ):
            if not lab.startswith("collective_"):
                continue
            cmd = self._fire_collective(side, lab, conf, sel, now)
            if cmd is not None:
                return cmd
        return None

    def _fire_collective(
        self,
        side: str,
        lab: str,
        conf: float,
        sel: Selection,
        now: float,
    ) -> GestureCommand | None:
        st = self._hands[side]
        if not self._debounced(st, lab):
            return None
        if st.episode_fired is not None:
            return None
        if lab in st.refractory_until and now < st.refractory_until[lab]:
            return None
        thr = self._threshold(side, lab) + self.cfg.start_margin
        if conf < thr:
            return None
        cmd = label_to_command(
            lab, side, sel, self._region_hist, self.n_regions, self.cfg
        )
        if cmd is None:
            return None
        # Lock down both hands' episodes — the gesture is collective.
        for s2 in ("L", "R"):
            self._hands[s2].episode_fired = lab
            self._hands[s2].release_ticks = 0
            self._hands[s2].refractory_until[lab] = now + self.cfg.per_class_refractory_s
        self._global_cooldown_until = now + self.cfg.global_cooldown_s
        return cmd

    def _threshold(self, side: str, label: str) -> float:
        bump = self.cfg.confidence_bump.get(label, 0.0)
        base = self.thresholds_by_side.get(side, {}).get(label, 0.65)
        return base + bump

    def _debounced(self, st: _HandState, label: str) -> bool:
        k = self.cfg.agreement_k
        hist = list(st.history)
        if len(hist) < k:
            return False
        return all(h == label for h in hist[-k:])


def label_to_command(
    label: str,
    side: str,
    sel: Selection,
    region_hist: dict[str, list[int | None]],
    n_regions: int,
    cfg: PostprocessConfig,
) -> GestureCommand | None:
    if label == "none":
        return None

    all_regions = tuple(range(n_regions))

    if label.startswith("collective_"):
        if label in ("collective_scale_up", "collective_scale_down"):
            step = 1 if label.endswith("up") else -1
            return GestureCommand("scale", all_regions, "both", steps=step)
        if label in ("collective_semitone_up", "collective_semitone_down"):
            step = 1 if label.endswith("up") else -1
            return GestureCommand("chromatic", all_regions, "both", steps=step)
        if label in ("collective_volume_up", "collective_volume_down"):
            step = 1 if label.endswith("up") else -1
            return GestureCommand("volume", all_regions, "both", steps=step)
        return None

    if label == "spawn_note":
        region, src = resolve_spawn_region(
            sel, region_hist, cfg.region_lookback, prefer=side
        )
        if region is None:
            return None
        return GestureCommand("spawn", (region,), src)

    if label in ("scale_up", "scale_down"):
        step = 1 if label.endswith("up") else -1
        region = _region_near(sel, side, region_hist[side], cfg.region_lookback)
        if region is None:
            return None
        return GestureCommand("scale", (region,), side, steps=step)

    if label in ("semitone_up", "semitone_down"):
        step = 1 if label.endswith("up") else -1
        region = _region_near(sel, side, region_hist[side], cfg.region_lookback)
        if region is None:
            return None
        return GestureCommand("chromatic", (region,), side, steps=step)

    return None


def resolve_spawn_region(
    sel: Selection,
    region_hist: dict[str, list[int | None]],
    lookback: int,
    prefer: str | None = None,
) -> tuple[int | None, str | None]:
    """Resolve which hand's pointed region a spawn should target.

    If ``prefer`` is given (the hand that actually fired the gesture), try it
    first so the OSC lands on the region the performer is pointing at with
    that hand. Falls back to the other hand only when the firing hand is
    out-of-plane.
    """
    if prefer in ("L", "R"):
        order = (prefer, "R" if prefer == "L" else "L")
    else:
        order = ("R", "L")
    for side in order:
        region = _region_near(sel, side, region_hist[side], lookback)
        if region is not None:
            return region, side
    return None, None


def _region_near(
    sel: Selection,
    side: str,
    history: list[int | None],
    lookback: int,
) -> int | None:
    arm = sel.arm(side)
    if arm.region is not None:
        return arm.region
    for r in reversed(history[-lookback:]):
        if r is not None:
            return r
    return None
