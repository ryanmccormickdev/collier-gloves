"""Constants and config for v3 hand-frame features."""
from __future__ import annotations

from dataclasses import dataclass

FEATURE_VERSION = "features_v3_handframe"

FEATURE_NAMES: tuple[str, ...] = (
    "vert_vel", "vel_along", "vel_across", "vel_horiz_mag", "lin_accel_mag",
    "wrist_tilt", "palm_roll", "forward_up", "openness",
    "wrist_tilt_rate", "palm_roll_rate",
    "gyro_along", "gyro_across", "gyro_palm",
    "accel_along", "accel_across", "accel_palm",
    "bend_thumb", "bend_index", "bend_middle", "bend_ring", "bend_pinky",
    "osc_thumb", "osc_index", "osc_middle", "osc_ring", "osc_pinky",
)
N_FEATURES = len(FEATURE_NAMES)

FINGER_NAMES = ("thumb", "index", "middle", "ring", "pinky")
FINGER_CHANNELS_L = {
    "thumb": (5, 2), "index": (7, 4), "middle": (0, 3), "ring": (1, 6), "pinky": (9, 8),
}
FINGER_CHANNELS_R = {
    "thumb": (9, 8), "index": (4, 3), "middle": (6, 0), "ring": (7, 1), "pinky": (5, 2),
}

N_CHANNELS = 10
RATE_HZ = 60.0
DT = 1.0 / RATE_HZ
STALE_AFTER_S = 0.4


@dataclass
class CondCfg:
    flex_env_decay_per_s: float = 0.0017
    flex_min_span: float = 80.0
    flex_bend_is_high: bool = True
    gyro_tau: float = 0.03
    orient_tau: float = 0.02
    openness_tau: float = 0.08
    wiggle_hp_tau: float = 0.08
    wiggle_env_attack_s: float = 0.03
    wiggle_env_release_s: float = 0.10
    twinkle_finger_osc_thresh: float = 0.04
    twinkle_min_active_fingers: int = 2
    twinkle_still_gyro_max: float = 3.5
    twinkle_randomness_gain: float = 4.0
    spawn_flutter_hp_tau: float = 0.06
    spawn_flutter_gain: float = 2.8
    spawn_still_gyro_max: float = 5.0
    spawn_env_attack_s: float = 0.02
    spawn_env_release_s: float = 0.08
    vel_bias_tau: float = 1.1
    vel_zero_gyro: float = 0.35
    vel_zero_accel: float = 0.5
    vel_zero_dwell_s: float = 0.15
    pitch_rate_tau: float = 0.05
    pitch_rate_spike_deg_s: float = 120.0
    finger_open_max: float = 0.50
