#!/usr/bin/env python3
"""Analyse a per-gesture live recording from ``apps/diag_live_record_gestures.py``.

For each intended gesture class:
  * Print the model's prediction histogram during the labeled segment.
  * Print per-feature mean (live) vs per-class training mean (train) and
    flag features with |z| >= 1.5 ("mismatch") or |z| >= 3 ("severe").
  * Show top-5 most discriminative shifts.

Run:
  python apps/diag_live_compare_gestures.py LIVE_RECORD.jsonl
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
    OSC_NAMES,
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


def stack_train_class(side: str, cls: str) -> np.ndarray:
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


def stack_live(records: list[dict], side: str, intended: str) -> np.ndarray:
    rows = []
    for r in records:
        if r.get("intended") != intended:
            continue
        d = r.get(side)
        if not d or not d.get("present"):
            continue
        row = d.get("feature_row")
        if row:
            rows.append(row)
    if not rows:
        return np.zeros((0, len(FEATURE_NAMES)), np.float32)
    return np.asarray(rows, dtype=np.float32)


def pred_hist(records: list[dict], side: str, intended: str) -> Counter:
    hist: Counter = Counter()
    for r in records:
        if r.get("intended") != intended:
            continue
        pred = r.get(side, {}).get("pred")
        if pred:
            hist[pred] += 1
    return hist


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dump", type=Path)
    ap.add_argument("--side", choices=("L", "R", "both"), default="both")
    args = ap.parse_args()
    meta, recs = load_dump(args.dump)
    print(f"\nDump: {args.dump.name}  classes={meta['classes']}  manifest={Path(meta['manifest']).name}")
    sides = ("L", "R") if args.side == "both" else (args.side,)

    classes_in_dump = [c for c in meta["classes"] if any(r.get("intended") == c for r in recs)]
    for cls in classes_in_dump:
        print(f"\n========== intended={cls!r} ==========")
        for side in sides:
            live = stack_live(recs, side, cls)
            if live.size == 0:
                print(f"  [{side}] no frames")
                continue
            # The training-side comparison class. For live none, compare to
            # training none; otherwise compare to the same class.
            train = stack_train_class(side, cls)
            hist = pred_hist(recs, side, cls)
            total = sum(hist.values())
            top = ", ".join(f"{k}:{v} ({100*v/total:.0f}%)"
                            for k, v in hist.most_common(5))
            correct_pct = 100 * hist.get(cls, 0) / max(total, 1)
            mark = "✓" if correct_pct > 50 else ("•" if correct_pct > 20 else "✗")
            print(f"  [{side}] {mark} model called {cls!r} on {correct_pct:.0f}% of ticks")
            print(f"        preds: {top}")
            if train.size == 0:
                print(f"  [{side}] no training data for {cls!r} to compare against")
                continue
            diffs = []
            for f in FEATURE_NAMES:
                i = FIDX[f]
                t_mu, t_sd = float(train[:, i].mean()), float(train[:, i].std()) + 1e-6
                l_mu = float(live[:, i].mean())
                diffs.append((f, l_mu, t_mu, (l_mu - t_mu) / t_sd))
            diffs.sort(key=lambda kv: -abs(kv[3]))
            severe = [d for d in diffs if abs(d[3]) >= 3.0]
            mild = [d for d in diffs if 1.5 <= abs(d[3]) < 3.0]
            if severe:
                print(f"  [{side}] SEVERE (|z|>=3) mismatches:")
                for f, l, t, z in severe:
                    print(f"        ‼ {f:<14} live={l:+.3f} train={t:+.3f}  z={z:+.2f}")
            if mild:
                print(f"  [{side}] mild (|z|>=1.5) mismatches:")
                for f, l, t, z in mild[:6]:
                    print(f"        ⚠ {f:<14} live={l:+.3f} train={t:+.3f}  z={z:+.2f}")


if __name__ == "__main__":
    main()
