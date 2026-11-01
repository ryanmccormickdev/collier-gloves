"""Shared helpers for diagnostic tools — keep these dependency-light."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
CLIPS_DIR = ROOT / "gesture_trainer_data" / "clips"
MODELS_DIR = ROOT / "gesture_trainer_data" / "models"

FEATURE_NAMES = (
    "vert_vel", "vel_along", "vel_across", "vel_horiz_mag", "lin_accel_mag",
    "wrist_tilt", "palm_roll", "forward_up", "openness",
    "wrist_tilt_rate", "palm_roll_rate",
    "gyro_along", "gyro_across", "gyro_palm",
    "accel_along", "accel_across", "accel_palm",
    "bend_thumb", "bend_index", "bend_middle", "bend_ring", "bend_pinky",
    "osc_thumb", "osc_index", "osc_middle", "osc_ring", "osc_pinky",
)
FIDX = {n: i for i, n in enumerate(FEATURE_NAMES)}
BEND_NAMES = ["bend_thumb", "bend_index", "bend_middle", "bend_ring", "bend_pinky"]
OSC_NAMES = ["osc_thumb", "osc_index", "osc_middle", "osc_ring", "osc_pinky"]


@dataclass
class Clip:
    path: Path
    mode: str
    gesture_class: str
    X_L: np.ndarray
    X_R: np.ndarray
    label_L: str
    start_L: int
    end_L: int
    labeled_L: bool
    label_R: str
    start_R: int
    end_R: int
    labeled_R: bool
    forward_L: float
    forward_R: float
    created_at: str

    def x(self, side: str) -> np.ndarray:
        return self.X_L if side == "L" else self.X_R

    def label(self, side: str) -> str:
        return self.label_L if side == "L" else self.label_R

    def span(self, side: str) -> tuple[int, int]:
        return (self.start_L, self.end_L) if side == "L" else (self.start_R, self.end_R)

    def t_created(self) -> float:
        try:
            return datetime.fromisoformat(self.created_at).timestamp()
        except Exception:
            return 0.0


def load_clip(path: Path) -> Clip:
    z = np.load(path, allow_pickle=True)
    return Clip(
        path=path,
        mode=str(z["mode"]),
        gesture_class=str(z["gesture_class"]),
        X_L=z["X_L"].astype(np.float32),
        X_R=z["X_R"].astype(np.float32),
        label_L=str(z["label_L"]),
        start_L=int(z["start_L"]),
        end_L=int(z["end_L"]),
        labeled_L=bool(z["labeled_L"]),
        label_R=str(z["label_R"]),
        start_R=int(z["start_R"]),
        end_R=int(z["end_R"]),
        labeled_R=bool(z["labeled_R"]),
        forward_L=float(z["forward_L"]) if "forward_L" in z.files else float("nan"),
        forward_R=float(z["forward_R"]) if "forward_R" in z.files else float("nan"),
        created_at=str(z["created_at"]) if "created_at" in z.files else "",
    )


def list_clips() -> list[Path]:
    return sorted(CLIPS_DIR.glob("*.npz"))


def latest_manifest() -> Path:
    cands = sorted(MODELS_DIR.glob("gesture_tcn_*.manifest.json"))
    if not cands:
        raise FileNotFoundError("No manifests in gesture_trainer_data/models")
    return cands[-1]


def active_manifest() -> Path:
    """Manifest pointed at by collier_live/models/active.json (the one in use live)."""
    active = ROOT / "collier_live" / "models" / "active.json"
    if active.exists():
        data = json.loads(active.read_text(encoding="utf-8"))
        p = data.get("manifest_path")
        if p:
            path = Path(p)
            if not path.is_absolute():
                path = ROOT / path
            if path.exists():
                return path
    return latest_manifest()


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def color_grade(val: float, lo: float, hi: float) -> str:
    if val <= lo:
        return f"\x1b[32m{val:.2f}\x1b[0m"
    if val >= hi:
        return f"\x1b[31m{val:.2f}\x1b[0m"
    return f"\x1b[33m{val:.2f}\x1b[0m"
