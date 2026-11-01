"""
Selection layer: "which regions am I addressing?"

Kept entirely separate from "what am I doing" (the gesture detectors). Each arm
independently resolves a pointed region and a pick (grab) state. Whole-body
poses — both palms up ("all"), or hands drawn in low ("timbre pose") — are
resolved here too. The recognizer asks this object for an event's targets.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .calibration import ReferenceFrame
from .conditioning import GloveSignals
from .config import SelectionConfig


@dataclass
class ArmSelection:
    present: bool = False
    heading: float | None = None        # deg relative to forward  (USED by gestures_scratch)
    region: int | None = None           # 0..n-1, or None if outside plane  (USED by gestures_scratch)
    # NOTE: gestures_scratch.py IGNORES `picked` and computes its own pick latch
    # locally (see ScratchRecognizer._compute_pick). This field is kept for the
    # legacy gestures.Recognizer only.
    picked: bool = False                # hand closed -> grabbing a region (legacy only)
    open_wide: bool = False             # legacy HUD flag


@dataclass
class Selection:
    left: ArmSelection = field(default_factory=ArmSelection)
    right: ArmSelection = field(default_factory=ArmSelection)
    all_mode: bool = False              # address every region at once
    timbre_pose: bool = False           # hands in low/centered -> global timbre

    # NOTE: `all_mode` and `timbre_pose` are whole-body pose STATES used by the
    # legacy gestures.Recognizer. gestures_scratch.py intentionally IGNORES them
    # and recomputes the equivalents locally (ScratchRecognizer._compute_all_mode
    # / _compute_timbre) so its whole gesture story lives in one file. They're
    # kept here only so the old Recognizer keeps working.

    def arm(self, side: str) -> ArmSelection:
        return self.left if side == "L" else self.right

    def targets(self, side: str, n_regions: int) -> tuple[int, ...]:
        """Regions an event from this arm should affect, given current state."""
        if self.all_mode:
            return tuple(range(n_regions))
        a = self.arm(side)
        if a.picked and a.region is not None:
            return (a.region,)
        if a.region is not None:
            return (a.region,)   # pointing is enough for twinkle-family events
        return ()


class PickLatch:
    """
    Pick engages when any non-thumb finger crosses finger_open_max while the
    hand is steady. It stays latched through arm motion until every non-thumb
    finger opens again (all below threshold).

    NOTE: only the legacy gestures.Recognizer consumes this. gestures_scratch.py
    reimplements the same logic locally (ScratchRecognizer._compute_pick) so it
    doesn't depend on selection-side state.
    """

    def __init__(self, open_max: float):
        self.open_max = open_max
        self.picked = False

    def update(self, sig: GloveSignals) -> bool:
        non_thumb = sig.finger_flex[1:]
        all_open = all(f <= self.open_max for f in non_thumb)
        any_bent = any(f > self.open_max for f in non_thumb)

        if not self.picked:
            if any_bent and sig.hand_steady:
                self.picked = True
        elif all_open:
            self.picked = False
        return self.picked


class AllModeLatch:
    """
    Enter when both hands are open AND turned palms-up (small |roll| on this
    hardware — a level palm-DOWN hand reads |roll| ≈ 180°); exit the instant
    either hand rolls back palm-down (or stops being open/present). Uses roll —
    stable — not pitch, with hysteresis. "Palms up + open = ALL; otherwise not."

    NOTE: only the legacy gestures.Recognizer consumes this. gestures_scratch.py
    reimplements it locally (ScratchRecognizer._compute_all_mode).
    """

    def __init__(self, cfg: SelectionConfig):
        self.cfg = cfg
        self.active = False

    def update(self, left: GloveSignals, right: GloveSignals) -> bool:
        c = self.cfg
        if not (left.present and right.present and left.is_open and right.is_open):
            self.active = False
            return False
        lr, rr = abs(left.roll), abs(right.roll)
        if not self.active:
            if lr > c.all_upside_down_roll_deg and rr > c.all_upside_down_roll_deg:
                self.active = True
        elif lr < c.all_exit_roll_deg or rr < c.all_exit_roll_deg:
            self.active = False
        return self.active


class SelectionResolver:
    def __init__(self, cfg: SelectionConfig):
        self.cfg = cfg
        self._pick = {
            "L": PickLatch(cfg.finger_open_max),
            "R": PickLatch(cfg.finger_open_max),
        }
        self._all_mode = AllModeLatch(cfg)
        # Always present; each hand's forward is unset until that hand calibrates.
        self.frame = ReferenceFrame(cfg)

    def calibrate(self, side: str, heading_deg: float) -> None:
        self.frame.set_forward(side, heading_deg)

    def is_calibrated(self, side: str) -> bool:
        return self.frame.is_calibrated(side)

    @property
    def calibrated(self) -> bool:
        """True once at least one hand has a forward (per-hand state in `frame`)."""
        return self.frame.is_calibrated("L") or self.frame.is_calibrated("R")

    def resolve(self, left: GloveSignals, right: GloveSignals) -> Selection:
        sel = Selection()
        sel.left = self._arm("L", left)
        sel.right = self._arm("R", right)

        # POSE DETECTION MOVED OUT: all_mode + timbre_pose are now computed
        # locally in gestures_scratch.ScratchRecognizer (_compute_all_mode /
        # _compute_timbre). selection.py is REGION-SELECTION ONLY. The flags are
        # left at their defaults (False) so this layer holds no pose "state".
        # (The legacy gestures.Recognizer, if you wire it back in, would need its
        # own pose detection too — the AllModeLatch class below is kept only as a
        # reference and is no longer invoked.)
        return sel

    def _arm(self, side: str, sig: GloveSignals) -> ArmSelection:
        a = ArmSelection(present=sig.present)
        if not sig.present:
            self._pick[side].picked = False
            return a
        a.picked = self._pick[side].update(sig)
        if self.frame.is_calibrated(side) and not math.isnan(sig.h_heading):
            a.heading = self.frame.heading(side, sig.h_heading)
            a.region = self.frame.region_of(side, sig.h_heading)
        return a
