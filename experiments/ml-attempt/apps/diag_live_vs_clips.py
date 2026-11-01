#!/usr/bin/env python3
"""Compare a live_dump JSONL capture to the training-clip distribution.

Run:  python apps/diag_live_vs_clips.py LIVE_DUMP.jsonl [--class scale_up]

Answers:
* Does the LIVE bend distribution match the TRAINING bend distribution for the
  same gesture? (Quantifies calibration mismatch when the model misbehaves.)
* What does the model predict on the live frames, and how does that compare to
  the labeled class' training-time predictions?
* Are the orientation / velocity features in the live capture in the same
  region as the training data?
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps._diag_shared import (
    BEND_NAMES,
    FEATURE_NAMES,
    FIDX,
    Clip,
    list_clips,
    load_clip,
)

KEY = ["wrist_tilt", "palm_roll", "forward_up", "openness",
       "vert_vel", "vel_along", "vel_across",
       "bend_thumb", "bend_index", "bend_middle", "bend_ring", "bend_pinky",
       "osc_index", "osc_middle"]


def load_dump(path: Path) -> tuple[dict, list[dict]]:
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    meta = json.loads(lines[0])["__meta__"]
    return meta, [json.loads(l) for l in lines[1:]]


def stack_class(side: str, cls: str) -> np.ndarray:
    rows = []
    for p in list_clips():
        c = load_clip(p)
        if c.label(side) != cls:
            continue
        s, e = c.span(side)
        x = c.x(side)
        if cls == "none":
            rows.append(x)
        elif e > s:
            rows.append(x[s:e])
    if not rows:
        return np.zeros((0, len(FEATURE_NAMES)), np.float32)
    return np.concatenate(rows, axis=0)


def stack_live(records: list[dict], side: str) -> np.ndarray:
    rows = [r[side]["feature_row"] for r in records
            if r.get(side, {}).get("present") and r[side]["feature_row"]]
    if not rows:
        return np.zeros((0, len(FEATURE_NAMES)), np.float32)
    return np.asarray(rows, dtype=np.float32)


def summarize(name: str, mat: np.ndarray) -> str:
    if mat.size == 0:
        return f"{name}: (empty)"
    parts = [f"{name}:"]
    for f in KEY:
        i = FIDX[f]
        parts.append(f"{f}={mat[:,i].mean():+.2f}±{mat[:,i].std():.2f}")
    return "  ".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dump", type=Path)
    ap.add_argument("--class", dest="cls", type=str, default=None,
                    help="Class to compare against (default: meta label or 'spawn_note').")
    args = ap.parse_args()
    meta, recs = load_dump(args.dump)
    cls = args.cls or meta["label"]
    print(f"\nLive dump: {args.dump.name}  n={len(recs)} ticks  label={meta['label']}")
    print(f"  manifest: {Path(meta['manifest']).name}")
    print(f"  live flex_range: {meta.get('live_flex_range')}")
    print(f"  comparing against training class: {cls}\n")

    for side in ("L", "R"):
        live = stack_live(recs, side)
        train = stack_class(side, cls)
        print(summarize(f"  TRAIN {side} ({cls})", train))
        print(summarize(f"  LIVE  {side} ({meta['label']})", live))

        if train.size and live.size:
            diff = []
            for f in KEY:
                i = FIDX[f]
                t_mu, t_sd = train[:, i].mean(), train[:, i].std() + 1e-6
                l_mu = live[:, i].mean()
                z = (l_mu - t_mu) / t_sd
                diff.append((f, z))
            diff.sort(key=lambda kv: -abs(kv[1]))
            print(f"  TOP feature mismatches (z = (live_mean - train_mean) / train_std):")
            for f, z in diff[:8]:
                marker = "  " if abs(z) < 1 else ("⚠ " if abs(z) < 2 else "‼ ")
                print(f"    {marker}{f:<14}  z={z:+.2f}")
        print()

    # Prediction histogram
    print("Live prediction histogram (per side):")
    for side in ("L", "R"):
        hist = Counter()
        for r in recs:
            p = r.get(side, {}).get("pred")
            if p:
                hist[p] += 1
        total = sum(hist.values())
        if not total:
            continue
        print(f"  {side}:")
        for lbl, n in hist.most_common():
            print(f"    {lbl:<26} {n:>5}  {100*n/total:5.1f}%")


if __name__ == "__main__":
    main()
