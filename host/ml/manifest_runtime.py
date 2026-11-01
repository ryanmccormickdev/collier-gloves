"""Load gesture manifests + per-hand PyTorch weights for live inference."""
from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from glove_ml_core.config import FEATURE_NAMES, N_FEATURES
from glove_ml_core.features import sig_features, validate_feature_names
from glove_ml_core.signals import GloveSignals

from .gesture_tcn import load_model, predict_proba_np


@dataclass
class HandDecision:
    label: str
    confidence: float
    probs: np.ndarray | None = None


class ManifestHandInferencer:
    """One hand: rolling window, z-score, TCN, threshold gate, refractory."""

    def __init__(
        self,
        manifest: dict,
        side: str,
        base_dir: Path,
        *,
        refractory_s: float = 0.4,
    ):
        self.side = side
        self.class_names: list[str] = list(manifest["class_names"])
        self.window_size = int(manifest["window_size"])
        fn = manifest.get("feature_names") or list(FEATURE_NAMES)
        validate_feature_names(fn)

        sd = manifest["sides"][side]
        self.thresholds = {str(k): float(v) for k, v in sd.get("class_thresholds", {}).items()}
        weights = Path(sd["model_weights_path"])
        if not weights.is_absolute():
            weights = base_dir / weights.name
        self.model = load_model(weights)

        mean = sd.get("feature_mean")
        std = sd.get("feature_std")
        self.mean = np.asarray(mean, dtype=np.float32) if mean else np.zeros(N_FEATURES, np.float32)
        self.std = np.asarray(std, dtype=np.float32) if std else np.ones(N_FEATURES, np.float32)

        self._frames: deque[list[float]] = deque(maxlen=self.window_size)
        self._refractory_until = 0.0
        self._last_fire_label = "none"
        self.refractory_s = refractory_s

    def reset(self) -> None:
        self._frames.clear()
        self._refractory_until = 0.0
        self._last_fire_label = "none"

    def update(self, sig: GloveSignals) -> HandDecision:
        row = sig_features(sig)
        self._frames.append(row)
        if len(self._frames) < self.window_size:
            return HandDecision(label="none", confidence=0.0, probs=None)

        x = ((np.asarray(self._frames, dtype=np.float32) - self.mean) / self.std)[None, :, :]
        probs = predict_proba_np(self.model, x)[0]
        idx = int(np.argmax(probs))
        conf = float(probs[idx])
        label = self.class_names[idx]

        if label != "none" and conf < self.thresholds.get(label, 0.65):
            label = "none"

        # Do not mask sustained classes to "none" here — collier_live postprocess
        # debounces and applies per-class refractory when emitting commands.

        return HandDecision(label=label, confidence=conf, probs=probs)


class DualManifestInferencer:
    def __init__(self, manifest_path: Path, *, refractory_s: float = 0.4):
        self.manifest_path = Path(manifest_path)
        self.base_dir = self.manifest_path.parent
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.class_names: list[str] = list(self.manifest["class_names"])
        self.window_size = int(self.manifest["window_size"])
        self.feature_version = str(self.manifest.get("feature_version", ""))
        self.model_arch = str(self.manifest.get("model_arch", ""))
        self.flex_range = self.manifest.get("flex_range") or {}
        self.hands = {
            s: ManifestHandInferencer(
                self.manifest, s, self.base_dir, refractory_s=refractory_s
            )
            for s in ("L", "R")
        }

    def reset(self) -> None:
        for h in self.hands.values():
            h.reset()

    def update(self, side: str, sig: GloveSignals) -> HandDecision:
        return self.hands[side].update(sig)

    def update_row(self, side: str, row: list[float]) -> HandDecision:
        """Update from a precomputed feature row (inspect tooling)."""
        dummy = GloveSignals(side=side, present=True)
        # Bypass sig_features by pushing row directly
        hand = self.hands[side]
        hand._frames.append(row)
        if len(hand._frames) < hand.window_size:
            return HandDecision(label="none", confidence=0.0, probs=None)
        x = ((np.asarray(hand._frames, dtype=np.float32) - hand.mean) / hand.std)[None, :, :]
        probs = predict_proba_np(hand.model, x)[0]
        idx = int(np.argmax(probs))
        conf = float(probs[idx])
        label = hand.class_names[idx]
        if label != "none" and conf < hand.thresholds.get(label, 0.65):
            label = "none"
        return HandDecision(label=label, confidence=conf, probs=probs)
