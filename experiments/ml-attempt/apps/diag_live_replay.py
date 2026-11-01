#!/usr/bin/env python3
"""Replay a recorded live dump through the *current* active manifest.

Loads each tick's ``feature_row`` (already conditioned, not re-computed) and
streams it through ``DualManifestInferencer.update_row`` so the freshly-patched
manifest can be evaluated against the same physical session that produced
the dump. Writes a new JSONL with updated ``pred``/``conf`` fields and prints
a quick per-class hit-rate summary that can be diffed against the original.

Use this to A/B a ``--mask-bends`` patch (or any manifest tweak) without
asking the user to put the gloves back on.

Run:
  python apps/diag_live_replay.py LIVE_RECORD.jsonl
  # optional:
  python apps/diag_live_replay.py LIVE_RECORD.jsonl \
    --manifest gesture_trainer_data/models/<NAME>.manifest.json \
    --out artifacts/reports/live_replays/replay_XXX.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps._diag_shared import active_manifest
from ml.manifest_runtime import DualManifestInferencer


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dump", type=Path)
    ap.add_argument("--manifest", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    mpath = args.manifest or active_manifest()
    print(f"Replaying with manifest: {mpath}")
    inf = DualManifestInferencer(mpath)
    print(f"  classes: {inf.class_names}")

    lines = args.dump.read_text(encoding="utf-8").strip().splitlines()
    meta_line = json.loads(lines[0])
    records = [json.loads(l) for l in lines[1:]]
    print(f"  dump:    {args.dump.name} ({len(records)} ticks)")

    by_intended_old: dict[str, Counter] = {}
    by_intended_new: dict[str, Counter] = {}

    out_records = []
    for r in records:
        intended = r.get("intended", "?")
        new_r = dict(r)
        for side in ("L", "R"):
            d = r.get(side)
            if not d or not d.get("present") or not d.get("feature_row"):
                continue
            old_pred = d.get("pred", "(warmup)")
            by_intended_old.setdefault(intended, Counter())[old_pred] += 1
            dec = inf.update_row(side, d["feature_row"])
            new_pred = dec.label if dec.probs is not None else "(warmup)"
            new_d = dict(d)
            new_d["pred"] = new_pred
            new_d["conf"] = float(dec.confidence) if dec.probs is not None else 0.0
            new_r[side] = new_d
            by_intended_new.setdefault(intended, Counter())[new_pred] += 1
        out_records.append(new_r)

    meta_line.setdefault("__meta__", {})["replayed_with"] = str(mpath)
    meta_line["__meta__"]["replayed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    out_path = args.out or args.dump.with_name(
        args.dump.stem + f"_replay_{int(time.time())}.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(meta_line) + "\n")
        for r in out_records:
            f.write(json.dumps(r) + "\n")
    print(f"\nWrote: {out_path}")

    print("\nPer-intended hit rates (old -> new):")
    print(f"  {'intended':<26} {'old%':>6} {'new%':>6} {'Δ':>6}")
    for cls in sorted(set(by_intended_old) | set(by_intended_new)):
        old = by_intended_old.get(cls, Counter())
        new = by_intended_new.get(cls, Counter())
        old_tot = sum(old.values()) or 1
        new_tot = sum(new.values()) or 1
        old_pct = 100 * old.get(cls, 0) / old_tot
        new_pct = 100 * new.get(cls, 0) / new_tot
        delta = new_pct - old_pct
        flag = "✓" if delta > 5 else ("✗" if delta < -5 else " ")
        print(f"  {flag} {cls:<24} {old_pct:>5.0f}% {new_pct:>5.0f}% {delta:>+5.0f}")

    print("\nTo see feature mismatches against the replayed preds:")
    print(f"  python apps/diag_live_compare_gestures.py {out_path}")


if __name__ == "__main__":
    main()
