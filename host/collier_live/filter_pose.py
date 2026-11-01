"""Gate ``/expr/filters`` OSC on a steady chest (or similar) pose.

Bands are pitch/roll/gyro from the Collier conditioner (degrees on pitch/roll).
No forward-heading calibration is required — only gravity-relative orientation.

Default bands are the **union** of two user-recorded poses (both validated live).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from collier.conditioning import GloveSignals


@dataclass
class FilterPoseSideConfig:
    pitch_lo: float
    pitch_hi: float
    roll_lo: float
    roll_hi: float
    gyro_mag_max: float
    lin_accel_mag_max: float = 1.5


def _default_L() -> FilterPoseSideConfig:
    # Union of two recorded chest poses (2026-06-02).
    return FilterPoseSideConfig(
        pitch_lo=-0.84,
        pitch_hi=43.99,
        roll_lo=-128.35,
        roll_hi=-84.94,
        gyro_mag_max=1.15,
    )


def _default_R() -> FilterPoseSideConfig:
    return FilterPoseSideConfig(
        pitch_lo=-44.31,
        pitch_hi=11.90,
        roll_lo=-127.21,
        roll_hi=-88.92,
        gyro_mag_max=1.15,
    )


@dataclass
class FilterPoseConfig:
    enabled: bool = True
    # Both hands must sit in-band + steady before streaming starts.
    require_both_hands: bool = True
    # Seconds pose conditions must hold continuously.
    hold_s: float = 0.75
    L: FilterPoseSideConfig = field(default_factory=_default_L)
    R: FilterPoseSideConfig = field(default_factory=_default_R)


def _in_band(v: float, lo: float, hi: float) -> bool:
    return lo <= v <= hi


class FilterPoseGate:
    """True when the performer has held the filter pose for ``hold_s``."""

    def __init__(self, cfg: FilterPoseConfig | None = None):
        self.cfg = cfg or FilterPoseConfig()
        self._hold_s = 0.0
        self.streaming = False

    def reset(self) -> None:
        self._hold_s = 0.0
        self.streaming = False

    def _hand_ok(self, sig: GloveSignals, side: str) -> bool:
        if not sig.present:
            return False
        c = self.cfg.L if side == "L" else self.cfg.R
        if not _in_band(sig.pitch, c.pitch_lo, c.pitch_hi):
            return False
        if not _in_band(sig.roll, c.roll_lo, c.roll_hi):
            return False
        if sig.gyro_mag > c.gyro_mag_max:
            return False
        if sig.lin_accel_mag > c.lin_accel_mag_max:
            return False
        return True

    def update(self, left: GloveSignals, right: GloveSignals, dt: float) -> bool:
        if not self.cfg.enabled:
            self.streaming = True
            return True

        l_ok = self._hand_ok(left, "L")
        r_ok = self._hand_ok(right, "R")
        in_pose = (l_ok and r_ok) if self.cfg.require_both_hands else (l_ok or r_ok)

        if in_pose:
            self._hold_s += dt
        else:
            self._hold_s = 0.0

        self.streaming = self._hold_s >= self.cfg.hold_s
        return self.streaming

    def progress(self) -> float:
        """0..1 fill for HUD while approaching hold_s."""
        if not self.cfg.enabled:
            return 1.0
        return min(1.0, self._hold_s / max(self.cfg.hold_s, 1e-6))
