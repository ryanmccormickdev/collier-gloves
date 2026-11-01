"""27-dim feature vector for gesture ML."""
from __future__ import annotations

from .config import FEATURE_NAMES, N_FEATURES
from .signals import GloveSignals

_SIG_LEN = N_FEATURES


def sig_features(sig: GloveSignals | None) -> list[float]:
    if sig is None or not sig.present:
        return [0.0] * _SIG_LEN
    fb = sig.finger_bend if sig.finger_bend else (0.0,) * 5
    fo = sig.finger_osc if sig.finger_osc else (0.0,) * 5
    hg = sig.hand_gyro
    ha = sig.hand_accel
    return [
        float(sig.vert_vel),
        float(sig.vel_along),
        float(sig.vel_across),
        float(sig.vel_horiz_mag),
        float(sig.lin_accel_mag),
        float(sig.wrist_tilt),
        float(sig.palm_roll),
        float(sig.forward_up),
        float(sig.openness),
        float(sig.wrist_tilt_rate),
        float(sig.palm_roll_rate),
        float(hg[0]),
        float(hg[1]),
        float(hg[2]),
        float(ha[0]),
        float(ha[1]),
        float(ha[2]),
        float(fb[0]),
        float(fb[1]),
        float(fb[2]),
        float(fb[3]),
        float(fb[4]),
        float(fo[0]),
        float(fo[1]),
        float(fo[2]),
        float(fo[3]),
        float(fo[4]),
    ]


def validate_feature_names(names: list[str] | tuple[str, ...]) -> None:
    if tuple(names) != FEATURE_NAMES:
        raise ValueError(
            f"Feature name mismatch: expected {FEATURE_NAMES}, got {tuple(names)}"
        )
