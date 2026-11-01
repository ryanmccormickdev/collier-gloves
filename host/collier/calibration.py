"""
Calibration: establishing the performer's reference frame (v3).

Only the horizontal heading is calibrated here. Pitch, roll, vertical accel, and
stroke detection stay gravity-relative and never depend on the stored forward.

Approach (v3): BOTH hands are calibrated at once with one deliberate gesture —
point both arms straight forward (level, open), hold still a beat, then flutter
(pick/open) each hand a few times to confirm. Each hand still stores its OWN
forward heading (captured from the still hold), so:

  * the two hands agree by construction (no averaging of disagreeing estimates,
    no mirrored-180° reconciliation),
  * independent magnetometer / gyro offsets cancel per hand, and
  * one hand alone never triggers it — but the flutter timing is loose, since
    the hands can't be picked in perfect lockstep.

Heading comes from the magnetometer-free game rotation vector, so it never
lurches from stage magnetic disturbance — it only drifts slowly. Re-strike the
pose (one hand or both) any time to re-zero.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .conditioning import GloveSignals
from .config import SelectionConfig
from .dsp import wrap180


def calib_pose_holding_side(sig: GloveSignals, cfg: SelectionConfig) -> bool:
    """
    One hand satisfies the point-forward calibration pose:
      - present with a defined heading,
      - hand open (fingers relaxed, thumb ignored via is_open),
      - arm near-horizontal (|pitch| small) so the heading axis is well-conditioned,
      - motionless (low rotation and translation),
      - game-RV fusion accuracy good enough to trust the heading.
    """
    if not sig.present:
        return False
    if math.isnan(sig.h_heading):
        return False
    s = cfg
    if not sig.is_open:
        return False
    if abs(sig.pitch) > s.calib_level_pitch_tol_deg:
        return False
    if sig.gyro_mag > s.calib_steady_gyro_max:
        return False
    ax, ay, az = sig.world_accel
    if math.hypot(ax, ay, az) > s.calib_max_world_accel:
        return False
    if sig.game_status < s.calib_min_game_status:
        return False
    return True


class CombinedCalibration:
    """
    A single deliberate TWO-HAND calibration. Both hands are calibrated at once;
    one hand alone never fires, but the flutter timing between hands is loose
    (each hand counts its own picks, we wait for the slower one).

      1. PRE-STILL : BOTH hands open + still + level (valid, healthy heading) for
                     calib_pre_still_s. At the end, both forward headings are
                     captured from this clean, pointed-forward pose.
      2. FLUTTER   : pick→open each hand calib_knock_cycles× — any timing, motion
                     ignored (squeezing rattles the IMU, that's fine). When BOTH
                     hands have completed their picks, the captured headings are
                     committed.

    Picks are detected from `openness` (robust: ~1.0 open, dips low on a pick)
    with hysteresis, NOT raw flex — a relaxed-open hand can sit mid-range on the
    flex sensors, which an absolute flex threshold misreads as "closed".

    `.update(left, right, dt)` returns `(left_heading, right_heading)` on the
    firing tick, else None. `.active` is True once past PRE-STILL. `.progress` is
    a 0..1 HUD bar: still 0→0.5, flutter 0.5→1.0.
    """

    WAIT, FLUTTER = 0, 1

    def __init__(self, cfg: SelectionConfig):
        self.cfg = cfg
        self.reset()

    def reset(self) -> None:
        self._state = self.WAIT
        self._wait_t = 0.0
        self._flutter_t = 0.0
        self._picks = {"L": 0, "R": 0}
        self._closed = {"L": False, "R": False}
        self._cand = {"L": None, "R": None}

    @property
    def active(self) -> bool:
        """Past the pre-still phase — i.e. mid-flutter."""
        return self._state != self.WAIT

    @property
    def progress(self) -> float:
        c = self.cfg
        if self._state == self.WAIT:
            return 0.5 * min(1.0, self._wait_t / max(c.calib_pre_still_s, 1e-6))
        done = min(self._picks["L"], self._picks["R"])
        return 0.5 + 0.5 * min(1.0, done / max(c.calib_knock_cycles, 1))

    def _hand_ready(self, sig: GloveSignals) -> bool:
        """Clean, still, level, pointed-forward — required of BOTH hands to arm
        and to define the captured heading."""
        c = self.cfg
        if not sig.present or math.isnan(sig.h_heading):
            return False
        if not sig.is_open:
            return False
        if abs(sig.pitch) > c.calib_level_pitch_tol_deg:
            return False
        if sig.gyro_mag > c.calib_steady_gyro_max:
            return False
        ax, ay, az = sig.world_accel
        if math.hypot(ax, ay, az) > c.calib_max_world_accel:
            return False
        if sig.game_status < c.calib_min_game_status:
            return False
        return True

    def _pick_edge(self, side: str, sig: GloveSignals) -> bool:
        """Hysteretic open->closed (pick) detector on openness."""
        c = self.cfg
        o = sig.openness
        if self._closed[side]:
            if o > c.calib_open_above:
                self._closed[side] = False
            return False
        if o < c.calib_pick_below:
            self._closed[side] = True
            return True
        return False

    def update(self, left: GloveSignals, right: GloveSignals,
               dt: float) -> tuple[float, float] | None:
        c = self.cfg
        sigs = {"L": left, "R": right}

        if self._state == self.WAIT:
            if self._hand_ready(left) and self._hand_ready(right):
                self._wait_t += dt
                if self._wait_t >= c.calib_pre_still_s:
                    self._cand["L"] = left.h_heading
                    self._cand["R"] = right.h_heading
                    self._state = self.FLUTTER
                    self._flutter_t = 0.0
                    self._picks = {"L": 0, "R": 0}
                    self._closed = {"L": False, "R": False}
            else:
                # Decay rather than hard-zero so a momentary blip on one hand
                # doesn't throw away an otherwise-steady hold.
                self._wait_t = max(0.0, self._wait_t - 2.0 * dt)
            return None

        # FLUTTER — only the picks matter; motion is ignored.
        if not (left.present and right.present
                and not math.isnan(left.h_heading)
                and not math.isnan(right.h_heading)):
            self.reset()
            return None
        self._flutter_t += dt
        if self._flutter_t > c.calib_knock_total_s:
            self.reset()                      # took too long — abandon
            return None
        for side in ("L", "R"):
            if self._picks[side] < c.calib_knock_cycles and self._pick_edge(side, sigs[side]):
                self._picks[side] += 1
        if min(self._picks["L"], self._picks["R"]) >= c.calib_knock_cycles:
            out = (self._cand["L"], self._cand["R"])
            self.reset()
            return out
        return None


@dataclass
class ReferenceFrame:
    """
    Maps each hand's horizontal heading -> heading relative to that hand's own
    calibrated forward. Forward is stored per side and may be unset (None).
    """

    cfg: SelectionConfig
    forward: dict[str, float | None] = field(
        default_factory=lambda: {"L": None, "R": None}
    )

    def set_forward(self, side: str, heading_deg: float) -> None:
        self.forward[side] = wrap180(heading_deg + self.cfg.forward_offset_deg)

    def is_calibrated(self, side: str) -> bool:
        return self.forward.get(side) is not None

    def heading(self, side: str, heading_deg: float) -> float | None:
        """Glove horizontal heading -> signed degrees relative to this hand's forward."""
        fwd = self.forward.get(side)
        if fwd is None:
            return None
        return wrap180(heading_deg - fwd)

    def region_of(self, side: str, heading_deg: float) -> int | None:
        """
        Bin a heading into one of n_regions across [-half, +half].
        Returns None if uncalibrated or pointing outside the plane.
        """
        h = self.heading(side, heading_deg)
        if h is None:
            return None
        half = self.cfg.plane_half_deg
        if abs(h) > half:
            return None

        n = self.cfg.n_regions
        width = (2.0 * half) / n
        raw = int((h + half) // width)
        return max(0, min(n - 1, raw))
