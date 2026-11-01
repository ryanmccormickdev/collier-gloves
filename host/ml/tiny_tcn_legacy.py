"""Legacy numpy TinyTCN weights (tcn_v2_meanmax) for old manifests only."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

LEGACY_ARCH = "tcn_v2_meanmax"


def softmax(logits: np.ndarray) -> np.ndarray:
    z = logits - logits.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / np.clip(e.sum(axis=1, keepdims=True), 1e-8, None)


@dataclass
class TinyTCNConfig:
    kernel_size: int = 5
    channels: int = 32


class LegacyTinyTCN:
    def __init__(self, in_features: int, n_classes: int, cfg: TinyTCNConfig):
        self.cfg = cfg
        k, c = cfg.kernel_size, cfg.channels
        self.Wc = np.zeros((k, in_features, c), np.float32)
        self.bc = np.zeros((c,), np.float32)
        self.Wc2 = np.zeros((k, c, c), np.float32)
        self.bc2 = np.zeros((c,), np.float32)
        self.Wo = np.zeros((2 * c, n_classes), np.float32)
        self.bo = np.zeros((n_classes,), np.float32)

    def _causal_conv(self, x: np.ndarray, Wc: np.ndarray, bc: np.ndarray) -> np.ndarray:
        b, t, _f = x.shape
        k, c = Wc.shape[0], Wc.shape[2]
        pad = k - 1
        xpad = np.pad(x, ((0, 0), (pad, 0), (0, 0)), mode="constant")
        conv = np.zeros((b, t, c), dtype=np.float32)
        for i in range(k):
            conv += xpad[:, i : i + t, :] @ Wc[k - 1 - i]
        return conv + bc.reshape(1, 1, -1)

    def _forward(self, x: np.ndarray):
        h1 = np.maximum(self._causal_conv(x, self.Wc, self.bc), 0.0)
        h2 = np.maximum(self._causal_conv(h1, self.Wc2, self.bc2), 0.0)
        pooled = np.concatenate([h2.mean(axis=1), h2.max(axis=1)], axis=1)
        return pooled @ self.Wo + self.bo

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        return softmax(self._forward(x.astype(np.float32)))

    @classmethod
    def load(cls, path: Path) -> "LegacyTinyTCN":
        data = np.load(path, allow_pickle=False)
        if "Wc2" not in data:
            raise ValueError(f"Not a legacy TinyTCN checkpoint: {path}")
        n_in = int(data["Wc"].shape[1])
        n_out = int(data["Wo"].shape[1])
        model = cls(n_in, n_out, TinyTCNConfig())
        model.Wc = data["Wc"].astype(np.float32)
        model.bc = data["bc"].astype(np.float32)
        model.Wc2 = data["Wc2"].astype(np.float32)
        model.bc2 = data["bc2"].astype(np.float32)
        model.Wo = data["Wo"].astype(np.float32)
        model.bo = data["bo"].astype(np.float32)
        return model
