"""HUD formatting for Collier ML Live GUI."""
from __future__ import annotations

import math

from collier.conditioning import GloveSignals
from collier.selection import ArmSelection

from ml.manifest_runtime import HandDecision


def _top_probs(class_names: list[str], probs, n: int = 3) -> str:
    if probs is None:
        return ""
    idx = sorted(range(len(probs)), key=lambda i: -probs[i])[:n]
    return " ".join(f"{class_names[i]}:{probs[i]:.2f}" for i in idx)


def format_ml_line(side: str, dec: HandDecision, class_names: list[str]) -> str:
    if dec.label == "none" and (dec.probs is None or dec.confidence < 0.01):
        return f"{side} ML: —"
    tops = _top_probs(class_names, dec.probs)
    return f"{side} ML: {dec.label} {dec.confidence:.2f}  [{tops}]"


def format_bend(sig: GloveSignals) -> str:
    if not sig.present:
        return "—"
    return " ".join(f"{int(b * 100):02d}" for b in sig.finger_bend[:5])


def format_flex_status(
    side: str,
    *,
    ready: bool,
    manifest_flex: bool,
    spans: list[float],
    label: str = "ML",
    window_s: float = 60.0,
) -> str:
    if manifest_flex:
        return f"{side} {label} flex: manifest"
    if not ready:
        return f"{side} {label} flex: awaiting packets"
    if not spans:
        return f"{side} {label} flex: {window_s:.0f}s min/max"
    avg = sum(spans) / len(spans)
    return f"{side} {label} flex: span avg {avg:.0f}"
