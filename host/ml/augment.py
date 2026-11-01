"""Training-time window augmentations (applied after z-score, on numpy windows)."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Indices aligned with glove_gesture_trainer.FEATURE_NAMES (features_v3_handframe).
BEND_IDX = slice(17, 22)
OSC_IDX = slice(22, 27)
WRIST_TILT_IDX = 5
PALM_ROLL_IDX = 6
WRIST_TILT_RATE_IDX = 9
PALM_ROLL_RATE_IDX = 10
ORIENT_IDX = [WRIST_TILT_IDX, PALM_ROLL_IDX, WRIST_TILT_RATE_IDX, PALM_ROLL_RATE_IDX]


@dataclass
class AugmentConfig:
    gaussian_noise: bool = False
    noise_std: float = 0.08

    bend_scale: bool = False
    bend_scale_lo: float = 0.85
    bend_scale_hi: float = 1.15

    bend_bias: bool = False
    bend_bias_std: float = 0.06

    time_warp: bool = False
    time_warp_lo: float = 0.9
    time_warp_hi: float = 1.1

    orientation_noise: bool = False
    orient_noise_std: float = 2.0

    channel_dropout: bool = False
    channel_dropout_prob: float = 0.15
    channel_dropout_frac: float = 0.12

    def any_enabled(self) -> bool:
        return any((
            self.gaussian_noise,
            self.bend_scale,
            self.bend_bias,
            self.time_warp,
            self.orientation_noise,
            self.channel_dropout,
        ))

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, d: dict | None) -> "AugmentConfig":
        if not d:
            return cls()
        fields = set(cls.__dataclass_fields__)
        return cls(**{k: d[k] for k in d if k in fields})


def augment_window(x: np.ndarray, cfg: AugmentConfig, rng: np.random.Generator) -> np.ndarray:
    """x shape (window, features); returns a new float32 copy."""
    y = np.array(x, dtype=np.float32, copy=True)
    t = y.shape[0]

    if cfg.bend_scale:
        s = rng.uniform(cfg.bend_scale_lo, cfg.bend_scale_hi)
        y[:, BEND_IDX] *= s
        y[:, OSC_IDX] *= s

    if cfg.bend_bias:
        b = rng.normal(0.0, cfg.bend_bias_std)
        y[:, BEND_IDX] += b
        y[:, OSC_IDX] += b * 0.5

    if cfg.time_warp and t > 4:
        scale = rng.uniform(cfg.time_warp_lo, cfg.time_warp_hi)
        new_t = max(4, min(t, int(round(t * scale))))
        src = np.linspace(0, t - 1, new_t)
        warped = np.zeros((t, y.shape[1]), dtype=np.float32)
        for f in range(y.shape[1]):
            warped[:, f] = np.interp(np.linspace(0, new_t - 1, t), src, y[:new_t, f]).astype(np.float32)
        y = warped

    if cfg.orientation_noise:
        y[:, ORIENT_IDX] += rng.normal(0.0, cfg.orient_noise_std, size=4).astype(np.float32)

    if cfg.channel_dropout:
        n_drop = max(1, int(round(y.shape[1] * cfg.channel_dropout_frac)))
        if rng.random() < cfg.channel_dropout_prob:
            cols = rng.choice(y.shape[1], size=min(n_drop, y.shape[1]), replace=False)
            y[:, cols] = 0.0

    if cfg.gaussian_noise:
        y += rng.normal(0.0, cfg.noise_std, size=y.shape).astype(np.float32)

    return y
