"""Load saved forward headings (optional) from gesture_trainer_data/calibration."""
from __future__ import annotations

import json
from pathlib import Path

from collier.selection import SelectionResolver

_REPO = Path(__file__).resolve().parents[1]
DEFAULT_CALIB_DIR = _REPO / "gesture_trainer_data" / "calibration"


def try_load_forward_calibration(
    resolver: SelectionResolver,
    calib_dir: Path | None = None,
) -> str | None:
    """
    If a JSON profile exists, set per-hand forward from ``forward_L`` / ``forward_R``.
    Returns the loaded filename, or None.
    """
    directory = calib_dir or DEFAULT_CALIB_DIR
    if not directory.is_dir():
        return None
    files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    path = files[0]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    loaded = False
    for side in ("L", "R"):
        val = payload.get(f"forward_{side}")
        if val is not None:
            resolver.calibrate(side, float(val))
            loaded = True
    return path.name if loaded else None
