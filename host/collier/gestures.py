"""
=============================================================================
 gestures.py  —  THE (OLD) GESTURE LAYER + THE MAP OF WHERE EACH GESTURE LIVES
=============================================================================

>>> If you are writing gestures from scratch, your blank canvas is
>>> `gestures_scratch.py`. THIS file is the *reference*: it documents exactly
>>> how the current (messy, agent-written) detectors work, what signal each
>>> reads, and the data overlaps each one is fighting. Read it, steal the good
>>> ideas, ignore the rest.

WHERE EACH GESTURE IS DETECTED  (the "point me to each one" you asked for)
-----------------------------------------------------------------------------
Every gesture is *detected* in code (nothing about a gesture is stored on disk;
recorded practice clips live in collier/recordings/, and the resulting musical
note state lives in music.py ChoirState). Detection map:

  GESTURE            EMITS            DETECTED IN (method)        SIGNAL IT READS
  -----------------  ---------------  --------------------------  ----------------------
  scale step         PitchStep        Recognizer._per_arm         sig.vert_vel  (SegmentStepper)
  semitone           PitchStep(chrom) Recognizer._per_arm         sig.pitch_rate (SegmentStepper)
  spawn / mute note  SpawnNote        Recognizer._twinkle         sig.spawn_flutter (WiggleEdge)
  timbre cycle       TimbreCycle      Recognizer._twinkle         min(wiggle L,R) in timbre pose
  ALL scale/chroma   PitchStep        Recognizer._all_mode        mean vert_vel / pitch_rate
  ALL volume         VolumeChange     Recognizer._all_mode        one hand twinkles, other's vert_vel
  (re)calibrate      Recalibrate      Recognizer._check_calibration -> calibration.py  (DO NOT TOUCH)

  Pose decisions that GATE the above (not gestures themselves) live in
  selection.py: PickLatch (a "grab"), AllModeLatch (palms-up), and the timbre
  pose test. Which region an arm points at is also selection.py + calibration.

  DEAD / DISABLED code, in the interest of honesty:
    - CircleDetector (below): a "louder = wave in a circle" detector. It only
      ever misfired and is DISABLED outside ALL mode. It's dead weight.
    - SustainPulse (event type): defined, handled by music.py, but NOTHING ever
      emits it. Vestigial.
    - VolumeChange outside ALL mode: removed for the same misfire reason.

DESIGN OF WHAT REMAINS
-----------------------------------------------------------------------------
Small, self-contained detectors consume per-hand `GloveSignals` and emit
candidate events; the `Recognizer` runs them, attaches target regions from the
Selection layer, and arbitrates so one physical gesture fires one event.

The pitch gestures (scale step + semitone) are both built on `SegmentStepper`
(see dsp): a motion is integrated into a per-segment displacement and emits a
quantized step when it rests or reverses. This is the key to staying *in sync* —
every coherent directional segment emits, so a raise→lower fires +1 then −1, a
slow move counts as much as a fast one, and a grand gesture jumps 2 steps.

THE HARD PART (and why this file is so convoluted): the raw signals OVERLAP.
A raise rotates the wrist; a flutter shakes the IMU and fakes a tilt; a grab
looks like a flutter. Most of the line count below is "dominant-modality"
arbitration trying to decide which ONE gesture a messy motion was. See the
"WHAT MIGHT BITE YOU" section in conditioning.py — those are the exact
ambiguities this code wrestles. Honestly, it's brittle; you'll likely do better
starting clean with the documented panel than trying to extend this.

Signal choices (the clean way to think about each):
  - scale step  : world-vertical TRANSLATION velocity      (segmented, distance-quantized)
  - semitone    : pitch TILT angle                          (segmented, 30°-quantized)
  - louder      : circular hand waving                      (DISABLED — see above)
  - twinkle     : multi-finger random motion while still    (spawn / timbre)
  - ALL mode    : both palms up + open; collective scale / chromatic / volume
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .calibration import CombinedCalibration
from .conditioning import GloveSignals
from .config import GestureConfig, SelectionConfig
from .dsp import Refractory, RollingSign, SegmentStepper, wrap180
from .selection import Selection


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
@dataclass
class GestureEvent:
    regions: tuple[int, ...]
    source: str = ""          # "L", "R", or "both" — for the debug HUD


@dataclass
class PitchStep(GestureEvent):
    steps: int = 0            # ±1, ±2, …
    chromatic: bool = False   # True = semitone, False = scale degree


@dataclass
class VolumeChange(GestureEvent):
    delta: float = 0.0


@dataclass
class SpawnNote(GestureEvent):
    pass


@dataclass
class TimbreCycle(GestureEvent):
    pass


@dataclass
class SustainPulse(GestureEvent):
    pass


@dataclass
class Recalibrate(GestureEvent):
    # Per-hand: a heading is present only for the hand(s) that completed the
    # calibration gesture this tick; None means "leave that hand's forward as-is".
    left_heading: float | None = None
    right_heading: float | None = None


# ---------------------------------------------------------------------------
# Detectors that aren't segment-based
# ---------------------------------------------------------------------------
class CircleDetector:
    """DEAD CODE (kept only for reference). "Louder = wave the hand in a circle":
    accumulates the circulation (running turn) of the horizontal accel vector.
    It misfired constantly on ordinary arm motion (any sweep looks a bit
    circular) and is no longer wired into the recognizer outside ALL mode. If
    you want a volume gesture, do NOT resurrect this — pick a cleaner signal."""

    def __init__(self, cfg: GestureConfig):
        self.cfg = cfg
        self._prev_angle: float | None = None
        self._accum = 0.0
        self._sign = RollingSign(n=20)

    def reset(self) -> None:
        self._prev_angle = None
        self._accum = 0.0

    @property
    def circulating(self) -> bool:
        return self._accum > 0.8

    def update(self, world_accel: tuple[float, float, float], dt: float) -> int:
        ax, ay, _ = world_accel
        mag = math.hypot(ax, ay)
        c = self.cfg
        if mag < c.circle_min_accel:
            self._accum *= 0.9
            self._prev_angle = None
            return 0
        angle = math.degrees(math.atan2(ay, ax))
        if self._prev_angle is None:
            self._prev_angle = angle
            return 0
        d = wrap180(angle - self._prev_angle)
        self._prev_angle = angle
        if self._sign.update(d) < c.circle_consistency:
            return 0
        self._accum += abs(math.radians(d))
        if self._accum >= c.circle_radians_per_step:
            self._accum -= c.circle_radians_per_step
            return 1
        return 0


class WiggleEdge:
    """Fires once when energy stays above threshold for min_hold_s."""

    def __init__(
        self,
        cfg: GestureConfig,
        *,
        fire_above: float | None = None,
        min_hold_s: float | None = None,
        reset_below: float | None = None,
        refractory_s: float | None = None,
    ):
        self.fire_above = fire_above if fire_above is not None else cfg.wiggle_fire_above
        self.min_hold_s = min_hold_s if min_hold_s is not None else cfg.wiggle_min_hold_s
        self.reset_below = reset_below if reset_below is not None else cfg.wiggle_reset_below
        self.refr = Refractory(refractory_s if refractory_s is not None else cfg.wiggle_refractory_s)
        self.armed = True
        self.above_for = 0.0

    def update(self, energy: float, dt: float) -> bool:
        self.refr.tick(dt)
        if energy > self.fire_above:
            self.above_for += dt
        elif energy < self.reset_below:
            self.above_for = 0.0
            self.armed = True
        if self.armed and self.above_for >= self.min_hold_s:
            self.armed = False
            return self.refr.fire()
        return False


# ---------------------------------------------------------------------------
# Recognizer
# ---------------------------------------------------------------------------
class Recognizer:
    """Runs all detectors, resolves targets via Selection, arbitrates, emits events."""

    def __init__(self, gcfg: GestureConfig, scfg: SelectionConfig):
        self.g = gcfg
        self.s = scfg
        self.n = scfg.n_regions

        self._scale = {s: self._make_scale_stepper() for s in ("L", "R")}
        self._tilt = {s: self._make_tilt_stepper() for s in ("L", "R")}
        self._circle = {"L": CircleDetector(gcfg), "R": CircleDetector(gcfg)}
        self._spawn_wig = {
            "L": WiggleEdge(gcfg, fire_above=gcfg.spawn_fire_above,
                           min_hold_s=gcfg.spawn_min_hold_s,
                           reset_below=gcfg.spawn_reset_below,
                           refractory_s=gcfg.spawn_refractory_s),
            "R": WiggleEdge(gcfg, fire_above=gcfg.spawn_fire_above,
                           min_hold_s=gcfg.spawn_min_hold_s,
                           reset_below=gcfg.spawn_reset_below,
                           refractory_s=gcfg.spawn_refractory_s),
        }
        self._timbre_wig = WiggleEdge(
            gcfg,
            fire_above=gcfg.timbre_wiggle_fire_above,
            min_hold_s=gcfg.wiggle_min_hold_s,
            reset_below=gcfg.wiggle_reset_below,
            refractory_s=gcfg.wiggle_refractory_s,
        )
        self._dwell = {"L": 0.0, "R": 0.0}
        self._point_region: dict[str, int | None] = {"L": None, "R": None}
        self._point_dwell = {"L": 0.0, "R": 0.0}
        self._pitch_latch: dict[str, int | None] = {"L": None, "R": None}
        self._pick_prev = {"L": False, "R": False}
        self._all_settle = 0.0
        self._play_lockout = 0.0
        self._calib = CombinedCalibration(scfg)

        # ALL mode: one stepper each on the mean of both hands.
        self._all_scale = self._make_scale_stepper(distance=True)
        self._all_tilt = self._make_tilt_stepper()

    # -- stepper factories -------------------------------------------------
    def _make_scale_stepper(self, distance: bool = False) -> SegmentStepper:
        g = self.g
        return SegmentStepper(
            move_rate=g.stroke_move_vel, stop_rate=g.stroke_stop_vel,
            stop_time_s=g.stroke_stop_time_s, reversal_rate=g.stroke_reversal_vel,
            unit_per_step=(g.all_stroke_dist_per_step if distance else g.stroke_dist_per_step),
            min_unit=(g.all_stroke_min_dist if distance else g.stroke_min_dist),
            max_steps=(g.all_stroke_max_steps if distance else g.stroke_max_steps),
        )

    def _make_tilt_stepper(self) -> SegmentStepper:
        g = self.g
        return SegmentStepper(
            move_rate=g.semitone_move_rate, stop_rate=g.semitone_stop_rate,
            stop_time_s=g.semitone_stop_time_s, reversal_rate=g.semitone_reversal_rate,
            unit_per_step=g.semitone_tilt_deg, min_unit=g.semitone_min_deg,
            max_steps=g.semitone_max_steps,
        )

    # -- main tick ---------------------------------------------------------
    def update(self, left: GloveSignals, right: GloveSignals,
               sel: Selection, dt: float) -> list[GestureEvent]:
        sigs = {"L": left, "R": right}
        events: list[GestureEvent] = []

        if self._play_lockout > 0.0:
            self._play_lockout = max(0.0, self._play_lockout - dt)

        # Calibration runs only when not in a whole-body pose and not right after
        # a musical event (so it can't arm mid-play). It does NOT short-circuit
        # playing gestures; everything else keeps detecting.
        recal = None
        if sel.all_mode or sel.timbre_pose or self._play_lockout > 0.0:
            self._calib.reset()
        else:
            recal = self._check_calibration(left, right, dt)

        self._update_dwell(sigs, dt)

        if sel.all_mode:
            events.extend(self._all_mode(left, right, dt))
        else:
            for side in ("L", "R"):
                events.extend(self._per_arm(side, sigs, sel, dt))
            events.extend(self._twinkle(sigs, sel, dt))

        if recal is not None:
            events.append(recal)

        out = [e for e in events if e is not None]
        if any(isinstance(e, (PitchStep, SpawnNote, VolumeChange, TimbreCycle))
               for e in out):
            self._play_lockout = self.s.calib_play_lockout_s
        return out

    # -- per-arm (normal mode) --------------------------------------------
    def _per_arm(self, side: str, sigs: dict[str, GloveSignals],
                 sel: Selection, dt: float) -> list[GestureEvent]:
        g = self.g
        sig = sigs[side]
        out: list[GestureEvent] = []
        arm = sel.arm(side)
        if not sig.present:
            self._scale[side].reset()
            self._tilt[side].reset()
            self._point_dwell[side] = 0.0
            self._point_region[side] = None
            self._pitch_latch[side] = None
            self._pick_prev[side] = False
            return out

        region = arm.region
        has_region = region is not None
        horiz = math.hypot(sig.world_accel[0], sig.world_accel[1])

        # Picking = grabbing a note on THIS hand only. The other hand can still
        # conduct pitch (common: hold a note with L, raise/lower with R).
        if arm.picked:
            if not self._pick_prev[side]:
                self._scale[side].reset()
                self._tilt[side].reset()
                self._point_dwell[side] = 0.0
                self._pitch_latch[side] = None
            self._pick_prev[side] = True
            return out
        self._pick_prev[side] = False

        # Point-dwell then latch: aim at a region, settle briefly, then lock the
        # target so heading drift during raise/lower doesn't reset the steppers.
        if region is None:
            self._scale[side].reset()
            self._tilt[side].reset()
            self._point_dwell[side] = 0.0
            self._pitch_latch[side] = None
            self._point_region[side] = None
            return out

        if region != self._point_region[side]:
            if self._pitch_latch[side] is None:
                self._scale[side].reset()
                self._tilt[side].reset()
                self._point_dwell[side] = 0.0
            self._point_region[side] = region

        if sig.gyro_mag < g.stroke_block_gyro:
            self._point_dwell[side] += dt
        else:
            self._point_dwell[side] = 0.0

        if self._pitch_latch[side] is None and self._point_dwell[side] >= g.pitch_arm_dwell_s:
            self._pitch_latch[side] = region

        point_ready = self._pitch_latch[side] is not None
        pitch_targets = (self._pitch_latch[side],) if point_ready else ()

        finger_busy = (sig.wiggle > g.wiggle_fire_above
                       or sig.spawn_flutter > g.spawn_fire_above)
        # Dominant modality: sf/w noise during an arm raise or wrist tilt doesn't
        # block pitch — only finger flutter while the arm is still does.
        scale_twinkle = (finger_busy
                         and abs(sig.vert_vel) < g.pitch_flutter_max_vert_vel)
        tilt_twinkle = (finger_busy
                        and abs(sig.pitch_rate) < g.pitch_flutter_max_pitch_rate)

        # ===== GESTURE: SCALE STEP (diatonic, PitchStep chromatic=False) =====
        # Reads sig.vert_vel (world up/down speed). A SegmentStepper integrates
        # it into distance and emits ±N when the motion rests or reverses.
        # OVERLAP it fights: a raise also tilts the wrist (pitch_rate) and spins
        # it (gyro), and a finger flutter fakes vertical velocity — hence the
        # pile of gates below (block on high pitch_rate, on sideways accel, on
        # finger flutter while the arm is still).
        # Scale step: pure vertical translation while pointing. Wrist rotation
        # during a raise/lower is normal (g can hit 4+); only block gyro when
        # the arm is still — same dominant-modality idea as sf/w twinkle gating.
        arm_conducting = abs(sig.vert_vel) >= g.stroke_move_vel
        scale_on = (point_ready and not scale_twinkle
                    and (arm_conducting or sig.gyro_mag < g.stroke_block_gyro)
                    and horiz < g.stroke_block_horiz_accel
                    and abs(sig.pitch_rate) < g.stroke_block_pitch_rate)
        steps = self._scale[side].update(sig.vert_vel, dt, enabled=scale_on)
        if steps and pitch_targets:
            out.append(PitchStep(regions=pitch_targets, source=side, steps=steps,
                                 chromatic=False))

        # ===== GESTURE: SEMITONE (chromatic, PitchStep chromatic=True) =====
        # Reads sig.pitch_rate (deg/s of wrist tilt). Same SegmentStepper trick,
        # 30°/step. OVERLAP it fights: a vertical raise changes pitch too, and a
        # flutter whips the wrist faster than any tilt — so it's blocked when
        # vert_vel is high (that's a scale move), when gyro is high (flutter),
        # and in the fingers-up zone (that's the timbre pose).
        # Semitone: pitch tilt while pointing, NOT translating.
        tilt_on = (point_ready and steps == 0 and not tilt_twinkle
                   and abs(sig.vert_vel) < g.semitone_block_vert_vel
                   and sig.gyro_mag < g.semitone_block_gyro
                   and sig.forward_up < g.semitone_block_fingers_up)
        tsteps = self._tilt[side].update(sig.pitch_rate, dt, enabled=tilt_on)
        if tsteps and pitch_targets:
            out.append(PitchStep(regions=pitch_targets, source=side, steps=tsteps,
                                 chromatic=True))

        # VolumeChange (circle waving) is DISABLED outside ALL mode — it only ever
        # fired errantly. Collective volume still lives in ALL mode (_all_mode).
        return out

    def _louder_targets(self, side: str, sel: Selection) -> tuple[int, ...]:
        other = sel.arm("R" if side == "L" else "L")
        if other.picked and other.region is not None:
            return (other.region,)
        return tuple(range(self.n))

    # -- twinkle family (normal mode) -------------------------------------
    def _update_dwell(self, sigs: dict[str, GloveSignals], dt: float) -> None:
        g = self.g
        for side, sig in sigs.items():
            # Only the arm must be roughly pointed (low gyro) — don't demand zero
            # vertical velocity; small settling drift is fine before the flutter.
            settled = sig.present and sig.gyro_mag < g.spawn_dwell_gyro_max
            self._dwell[side] = self._dwell[side] + dt if settled else 0.0

    def _twinkle(self, sigs: dict[str, GloveSignals], sel: Selection,
                 dt: float) -> list[GestureEvent]:
        g = self.g
        left, right = sigs["L"], sigs["R"]

        # ===== GESTURE: TIMBRE CYCLE (TimbreCycle) =====
        # Both hands in the "fingers-up + open" pose (resolved in selection.py),
        # then wiggle BOTH hands. Reads each hand's `wiggle`/`spawn_flutter`
        # energy and fires on the weaker of the two (min) so one twitchy hand
        # can't trigger it alone.
        # Timbre takes priority: both hands fingers-up + open, wiggle both to fire.
        if sel.timbre_pose:
            lw = left.wiggle if left.present else 0.0
            rw = right.wiggle if right.present else 0.0
            # Accept either independent multi-finger wiggle OR coordinated flutter.
            l_tw = max(lw, left.spawn_flutter if left.present else 0.0)
            r_tw = max(rw, right.spawn_flutter if right.present else 0.0)
            both = min(l_tw, r_tw)
            if self._timbre_wig.update(both, dt):
                return [TimbreCycle(regions=tuple(range(self.n)), source="both")]
            return []

        # ===== GESTURE: SPAWN / MUTE NOTE (SpawnNote) =====
        # Point an arm at a region, settle ~0.15 s, then flutter that hand's
        # fingers. Reads sig.spawn_flutter (a WiggleEdge edge-detector). This is
        # the single most misfire-prone gesture in the old code: spawn_flutter
        # also rises on a finger GRAB and on envelope decay AFTER a flutter, so
        # there are extra guards (require live sf at the fire tick, a settled
        # arm, the other hand not also fluttering, etc.). Brittle.
        any_picked = sel.left.picked or sel.right.picked
        # Per-hand spawn: point at a region, settle a moment, then twinkle. The
        # only requirement on the OTHER hand is that it isn't ALSO twinkling —
        # that case is reserved for timbre / collective gestures. (We don't
        # demand it be still & open; that proved too fiddly to hit live.)
        out: list[GestureEvent] = []
        for side in ("L", "R"):
            sig = sigs[side]
            other_side = "R" if side == "L" else "L"
            other = sigs[other_side]
            fired = self._spawn_wig[side].update(
                sig.spawn_flutter if sig.present else 0.0, dt)
            if not fired or not sig.present or self._calib.active:
                continue
            # WiggleEdge can fire on envelope decay after sf has already dropped
            # (the errant spawns at sf≈0.11). Require a live, deliberate flutter
            # and a settled arm at the moment of fire.
            if (sig.spawn_flutter < g.spawn_fire_above
                    or abs(sig.vert_vel) > g.spawn_max_vert_vel):
                continue
            arm = sel.arm(side)
            if arm.region is None or self._dwell[side] < g.spawn_point_dwell_s:
                continue
            # While either hand is grabbing/moving a note, suppress incidental
            # spawns on the other hand entirely; on the picking hand only allow
            # a strong flutter while the arm is still (intentional spawn-while-pick).
            if any_picked:
                if not arm.picked:
                    continue
                if (sig.spawn_flutter < g.spawn_pick_min_sf
                        or abs(sig.vert_vel) > g.spawn_pick_max_vert_vel
                        or sig.gyro_mag > g.spawn_pick_max_gyro):
                    continue
            other_ok = (not other.present
                        or other.spawn_flutter <= g.spawn_fire_above)
            if not other_ok:
                continue
            out.append(SpawnNote(regions=(arm.region,), source=side))
            break  # one spawn per tick
        return out

    # -- ALL mode ----------------------------------------------------------
    def _all_mode(self, left: GloveSignals, right: GloveSignals,
                  dt: float) -> list[GestureEvent]:
        g = self.g
        all_regions = tuple(range(self.n))
        out: list[GestureEvent] = []

        # Hold everything in reset until the palms-up FLIP is over: the wrists
        # spin hard entering the pose and that transient integrates into a huge
        # phantom velocity. Only once both hands are calm for all_settle_s do
        # collective gestures arm — this is what stops volume/pitch firing on
        # the flip instead of on your deliberate move.
        if not (left.present and right.present):
            self._all_scale.reset()
            self._all_tilt.reset()
            self._all_settle = 0.0
            return out
        if max(left.gyro_mag, right.gyro_mag) > g.all_settle_gyro:
            self._all_settle = 0.0
        else:
            self._all_settle += dt
        if self._all_settle < g.all_settle_s:
            self._all_scale.reset()
            self._all_tilt.reset()
            return out

        lw = left.wiggle if left.present else 0.0
        rw = right.wiggle if right.present else 0.0
        l_tw = lw > g.wiggle_fire_above
        r_tw = rw > g.wiggle_fire_above

        # Collective volume: exactly one hand twinkles, the OTHER (open) hand's
        # vertical movement is the fader. Suppresses scale/chromatic meanwhile.
        if l_tw != r_tw:
            self._all_scale.reset()
            self._all_tilt.reset()
            ctrl = right if l_tw else left
            if ctrl.present and ctrl.is_open:
                delta = max(-g.all_volume_delta,
                            min(g.all_volume_delta, ctrl.vert_vel * g.all_volume_vel_gain))
                if abs(delta) > 1e-4:
                    out.append(VolumeChange(regions=all_regions, source="both", delta=delta))
            return out

        if not (left.present and right.present) or (l_tw and r_tw):
            self._all_scale.reset()
            self._all_tilt.reset()
            return out

        # Collective scale / chromatic: both hands move in sync (mean motion;
        # opposite motions cancel and won't fire).
        mean_vert = 0.5 * (left.vert_vel + right.vert_vel)
        mean_pr = 0.5 * (left.pitch_rate + right.pitch_rate)
        same_vert = (left.vert_vel >= 0) == (right.vert_vel >= 0)
        same_pr = (left.pitch_rate >= 0) == (right.pitch_rate >= 0)

        scale_on = (same_vert
                    and max(left.gyro_mag, right.gyro_mag) < g.stroke_block_gyro
                    and abs(mean_pr) < g.stroke_block_pitch_rate)
        steps = self._all_scale.update(mean_vert, dt, enabled=scale_on)
        if steps:
            out.append(PitchStep(regions=all_regions, source="both", steps=steps, chromatic=False))
            self._all_tilt.update(0.0, dt, enabled=False)
            return out

        tilt_on = (same_pr and abs(mean_vert) < g.semitone_block_vert_vel
                   and max(left.gyro_mag, right.gyro_mag) < g.semitone_block_gyro
                   and left.forward_up < g.semitone_block_fingers_up
                   and right.forward_up < g.semitone_block_fingers_up)
        tsteps = self._all_tilt.update(mean_pr, dt, enabled=tilt_on)
        if tsteps:
            out.append(PitchStep(regions=all_regions, source="both", steps=tsteps, chromatic=True))
        return out

    # -- calibration -------------------------------------------------------
    def _check_calibration(self, left: GloveSignals, right: GloveSignals,
                           dt: float) -> Recalibrate | None:
        captured = self._calib.update(left, right, dt)
        if captured is not None:
            lh, rh = captured
            return Recalibrate(regions=(), source="both",
                               left_heading=lh, right_heading=rh)
        return None

    @property
    def calib_progress(self) -> dict[str, float]:
        return {"B": self._calib.progress}
