"""V3 hand-frame features for gesture ML (shared by trainer parity + collier_live)."""

from .config import (
    CondCfg,
    DT,
    FEATURE_NAMES,
    FEATURE_VERSION,
    FINGER_CHANNELS_L,
    FINGER_CHANNELS_R,
    FINGER_NAMES,
    N_CHANNELS,
    N_FEATURES,
    RATE_HZ,
    STALE_AFTER_S,
)
from .features import sig_features
from .flex_live import RollingFlexTracker, SessionFlexTracker
from .signals import GloveConditioner, GloveSignals

__all__ = [
    "CondCfg",
    "DT",
    "FEATURE_NAMES",
    "FEATURE_VERSION",
    "FINGER_CHANNELS_L",
    "FINGER_CHANNELS_R",
    "FINGER_NAMES",
    "GloveConditioner",
    "GloveSignals",
    "N_CHANNELS",
    "N_FEATURES",
    "RATE_HZ",
    "RollingFlexTracker",
    "SessionFlexTracker",
    "STALE_AFTER_S",
    "sig_features",
]
