#!/usr/bin/env python3
"""Dataset-level diagnosis: drift over the recording session, per-class feature
stats, and pairwise class separability.

Run:  python apps/diag_dataset.py [--out diag_dataset_report.txt]

What it reveals:
* Whether the rolling flex envelope normalization drifted across the recording
  session (bend means in early vs late clips of the same class).
* Per-(class, side) means/stds for the most-discriminative features.
* For each confusable class pair, a simple per-feature separability score
  (|mean_a - mean_b| / (std_a + std_b)) — a feature with score > ~0.8 actually
  helps the classifier; near zero means the model has no signal to separate.
* How many clips per (label_L, label_R) pair, total frames, "none" sources.

This script reads only the saved feature clips; no glove needed.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps._diag_shared import (
    BEND_NAMES,
    FEATURE_NAMES,
    FIDX,
    OSC_NAMES,
    Clip,
    list_clips,
    load_clip,
)

CONFUSABLE_PAIRS: list[tuple[str, str]] = [
    ("scale_up", "scale_down"),
    ("scale_up", "collective_scale_up"),
    ("scale_down", "collective_scale_down"),
    ("scale_up", "semitone_up"),
    ("scale_down", "semitone_down"),
    ("semitone_up", "semitone_down"),
    ("collective_semitone_up", "collective_semitone_down"),
    ("collective_scale_up", "collective_semitone_up"),
    ("collective_scale_down", "collective_semitone_down"),
    ("spawn_note", "none"),
    ("spawn_note", "scale_up"),
    ("scale_up", "none"),
    ("collective_volume_up", "collective_scale_up"),
]

# Features most likely to carry signal — used for the readout.
KEY_FEATS = [
    "wrist_tilt", "palm_roll", "forward_up", "openness",
    "vert_vel", "vel_along", "vel_across",
    "gyro_along", "gyro_across", "gyro_palm",
    "wrist_tilt_rate", "palm_roll_rate",
    "bend_index", "bend_middle", "bend_ring", "bend_pinky", "bend_thumb",
    "osc_index", "osc_middle",
]


def _stack_class_frames(clips: list[Clip], cls: str, side: str) -> np.ndarray:
    rows = []
    for c in clips:
        if c.label(side) != cls:
            continue
        s, e = c.span(side)
        x = c.x(side)
        if cls == "none":
            rows.append(x)
        elif e > s:
            rows.append(x[s:e])
    if not rows:
        return np.zeros((0, len(FEATURE_NAMES)), dtype=np.float32)
    return np.concatenate(rows, axis=0)


def _per_clip_means(clips: list[Clip], cls: str, side: str, feat: str) -> list[tuple[float, float]]:
    """For each clip of (cls, side), (created_ts, in-span mean of feature)."""
    out = []
    fi = FIDX[feat]
    for c in clips:
        if c.label(side) != cls:
            continue
        s, e = c.span(side)
        x = c.x(side)
        seg = x if cls == "none" else (x[s:e] if e > s else x[:0])
        if seg.size == 0:
            continue
        out.append((c.t_created(), float(seg[:, fi].mean())))
    out.sort()
    return out


def _separability(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """|mu_a - mu_b| / (sigma_a + sigma_b + eps), per feature."""
    if a.size == 0 or b.size == 0:
        return np.zeros(len(FEATURE_NAMES), dtype=np.float32)
    mu_a, mu_b = a.mean(axis=0), b.mean(axis=0)
    sd_a, sd_b = a.std(axis=0) + 1e-6, b.std(axis=0) + 1e-6
    return np.abs(mu_a - mu_b) / (sd_a + sd_b)


def _print_class_table(clips: list[Clip], outlines: list[str]) -> None:
    headers = ["class", "side", "N_frames"] + KEY_FEATS
    outlines.append("\n=== Per-class in-span MEAN of each feature (key features only) ===")
    outlines.append("  format: mean ± std")
    rule = "-" * 8
    outlines.append(f"  {'class':<26} {'side':4} {'frames':>7}  " + "  ".join(f"{f:>16}" for f in KEY_FEATS))
    by_class: dict[str, dict[str, np.ndarray]] = defaultdict(dict)
    for cls in sorted({c.label("L") for c in clips} | {c.label("R") for c in clips}):
        for side in ("L", "R"):
            mat = _stack_class_frames(clips, cls, side)
            by_class[cls][side] = mat
            if mat.shape[0] == 0:
                continue
            cells = []
            for f in KEY_FEATS:
                fi = FIDX[f]
                cells.append(f"{mat[:, fi].mean():+6.2f}±{mat[:, fi].std():.2f}")
            outlines.append(
                f"  {cls:<26} {side:4} {mat.shape[0]:>7}  " + "  ".join(f"{c:>16}" for c in cells)
            )


def _print_separability(clips: list[Clip], outlines: list[str]) -> None:
    outlines.append("\n=== Class-pair feature separability (key features) ===")
    outlines.append("  Higher = better (>0.8 generally usable, >1.5 strong, <0.3 useless).")
    outlines.append("  Each row shows top-5 features for that pair, per side.")
    for a, b in CONFUSABLE_PAIRS:
        for side in ("L", "R"):
            xa = _stack_class_frames(clips, a, side)
            xb = _stack_class_frames(clips, b, side)
            if xa.size == 0 or xb.size == 0:
                continue
            sep = _separability(xa, xb)
            top = np.argsort(-sep)[:5]
            parts = [f"{FEATURE_NAMES[i]}={sep[i]:.2f}" for i in top]
            outlines.append(f"  {a} ↔ {b}  [{side}]  -> " + "  ".join(parts))


def _print_drift(clips: list[Clip], outlines: list[str]) -> None:
    """Show per-class bend drift: does mean bend value trend over time?"""
    outlines.append("\n=== Recording-session drift (bend means by clip timestamp) ===")
    outlines.append("  Per (class, side, finger): first-quarter mean vs last-quarter mean.")
    outlines.append("  Large delta = bend normalization drifted while recording.")
    outlines.append(
        f"  {'class':<26} {'side':4} {'finger':<14} "
        f"{'1st_q':>8} {'last_q':>8} {'delta':>8} {'n_clips':>8}"
    )
    for cls in sorted({c.label("L") for c in clips}):
        if cls == "none":
            continue
        for side in ("L", "R"):
            for feat in BEND_NAMES:
                series = _per_clip_means(clips, cls, side, feat)
                if len(series) < 8:
                    continue
                vals = np.array([v for _, v in series])
                q = len(vals) // 4
                early = vals[:q].mean() if q else vals[0]
                late = vals[-q:].mean() if q else vals[-1]
                outlines.append(
                    f"  {cls:<26} {side:4} {feat:<14} "
                    f"{early:>8.2f} {late:>8.2f} {late-early:>+8.2f} {len(vals):>8}"
                )


def _print_pair_counts(clips: list[Clip], outlines: list[str]) -> None:
    outlines.append("\n=== Mode / label counts ===")
    modes = Counter(c.mode for c in clips)
    for m, n in modes.most_common():
        outlines.append(f"  mode {m}: {n}")
    pair = Counter((c.label_L, c.label_R) for c in clips)
    outlines.append("\n  (label_L, label_R) pairs:")
    for k, v in sorted(pair.items()):
        outlines.append(f"    {k}: {v}")


def _print_span_stats(clips: list[Clip], outlines: list[str]) -> None:
    outlines.append("\n=== Clip / span length stats ===")
    n_frames = [c.X_L.shape[0] for c in clips]
    spans_l = [c.end_L - c.start_L for c in clips if c.end_L > c.start_L]
    spans_r = [c.end_R - c.start_R for c in clips if c.end_R > c.start_R]
    outlines.append(
        f"  frames  min={min(n_frames)} max={max(n_frames)} mean={np.mean(n_frames):.1f}"
        f"  median={int(np.median(n_frames))}"
    )
    outlines.append(
        f"  span_L  min={min(spans_l)} max={max(spans_l)} mean={np.mean(spans_l):.1f}"
        f"  median={int(np.median(spans_l))}"
    )
    outlines.append(
        f"  span_R  min={min(spans_r)} max={max(spans_r)} mean={np.mean(spans_r):.1f}"
        f"  median={int(np.median(spans_r))}"
    )
    # Long-tail / suspicious clips
    long_clips = sorted(
        [(c.path.name, n_frames[i]) for i, c in enumerate(clips) if n_frames[i] > 400],
        key=lambda t: -t[1],
    )[:10]
    if long_clips:
        outlines.append("  long clips (>400 frames, may have extra junk):")
        for name, t in long_clips:
            outlines.append(f"    {t:5}  {name}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=None,
                    help="Optional file to write the full report to.")
    args = ap.parse_args()

    paths = list_clips()
    if not paths:
        print("No clips found.")
        return
    clips = [load_clip(p) for p in paths]

    lines: list[str] = []
    lines.append(f"Dataset: {len(clips)} clips from {paths[0].parent}")
    fwd_L = sorted({float(c.forward_L) for c in clips if c.forward_L == c.forward_L})
    fwd_R = sorted({float(c.forward_R) for c in clips if c.forward_R == c.forward_R})
    lines.append(
        f"forward_L distinct={len(fwd_L)} (e.g. {fwd_L[:3]})  "
        f"forward_R distinct={len(fwd_R)} (e.g. {fwd_R[:3]})"
    )
    if len(fwd_L) == 1 and len(fwd_R) == 1:
        lines.append("  -> Single fixed orientation calibration across the whole dataset.")

    _print_pair_counts(clips, lines)
    _print_span_stats(clips, lines)
    _print_class_table(clips, lines)
    _print_drift(clips, lines)
    _print_separability(clips, lines)

    report = "\n".join(lines)
    print(report)
    if args.out:
        args.out.write_text(report + "\n", encoding="utf-8")
        print(f"\nWrote report -> {args.out}")


if __name__ == "__main__":
    main()
