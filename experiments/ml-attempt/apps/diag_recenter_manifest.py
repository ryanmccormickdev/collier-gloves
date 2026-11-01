#!/usr/bin/env python3
"""Patch a manifest's per-feature mean to match a live capture, fixing the
"3-hour vs 30-second envelope" calibration mismatch documented in
``docs/diagnosis_findings.md``.

The trained model z-scores inputs by ``(x - feature_mean) / feature_std``.
If the live distribution of a feature is *centered* somewhere different from
where the training distribution was centered (which happens with bends here
because the EnvelopeMinMax converged to different widths), the z-scored
inputs look completely off-distribution to the model. By shifting
``feature_mean`` to match the live center for the affected features, the
*gesture dynamics* (deltas from rest, oscillation patterns, etc.) stay
intact and the model sees structurally-compatible inputs again.

Only bend/osc features are shifted by default. Orientation/velocity features
should already be in the same regime — if they aren't, you have a forward-
calibration problem instead and this tool won't help.

Run:
  python apps/diag_recenter_manifest.py \\
    artifacts/reports/live_dumps/live_dump_probe_XXX.jsonl \\
    [--manifest gesture_trainer_data/models/<MANIFEST>.manifest.json] \\
    [--all-features]   # shift every feature, not just bends/oscs (riskier)
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
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
    active_manifest,
    load_manifest,
)


BEND_OSC_IDX = [FIDX[n] for n in BEND_NAMES + OSC_NAMES]
BEND_IDX = [FIDX[n] for n in BEND_NAMES]


def _none_means_per_side(records: list[dict]) -> dict[str, np.ndarray]:
    """Per-side mean of feature_row across ticks where intended=='none'."""
    out: dict[str, list[list[float]]] = {"L": [], "R": []}
    for r in records:
        if r.get("intended") != "none":
            continue
        for side in ("L", "R"):
            d = r.get(side)
            if d and d.get("present") and d.get("feature_row"):
                out[side].append(d["feature_row"])
    means = {}
    for side in ("L", "R"):
        if out[side]:
            means[side] = np.asarray(out[side], dtype=np.float32).mean(axis=0)
    return means


def _mask_features(manifest: dict, idxs: list[int], huge: float = 1e6) -> dict:
    """Set feature_std[idx] = huge for each idx, which causes the z-score for
    that feature to be effectively zero regardless of input — the model now
    sees a constant flat input for those dimensions."""
    for side in ("L", "R"):
        sd = manifest["sides"][side]
        std = np.asarray(sd["feature_std"], dtype=np.float32).copy()
        for i in idxs:
            std[i] = huge
        sd["feature_std"] = std.astype(float).tolist()
        print(f"  [{side}] masked {len(idxs)} feature_std -> ~0 z-score: "
              f"{[FEATURE_NAMES[i] for i in idxs]}")
    manifest.setdefault("calib_patches", []).append({
        "via": "apps/diag_recenter_manifest.py --mask",
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "masked_features": [FEATURE_NAMES[i] for i in idxs],
    })
    return manifest


def _live_means(records: list[dict]) -> dict[str, np.ndarray]:
    out: dict[str, list[list[float]]] = {"L": [], "R": []}
    for r in records:
        for side in ("L", "R"):
            d = r.get(side)
            if not d or not d.get("present"):
                continue
            row = d.get("feature_row")
            if row:
                out[side].append(row)
    means = {}
    for side in ("L", "R"):
        if not out[side]:
            continue
        mat = np.asarray(out[side], dtype=np.float32)
        means[side] = mat.mean(axis=0)
    return means


def _patch(manifest: dict, live_means: dict[str, np.ndarray], idxs: list[int]) -> dict:
    for side in ("L", "R"):
        sd = manifest["sides"][side]
        mean = np.asarray(sd["feature_mean"], dtype=np.float32).copy()
        if side not in live_means:
            print(f"  [{side}] no live frames; skipping")
            continue
        live = live_means[side]
        old = mean.copy()
        for i in idxs:
            mean[i] = float(live[i])
        sd["feature_mean"] = mean.astype(float).tolist()
        print(f"  [{side}] patched {len(idxs)} feature means:")
        for i in idxs:
            print(f"      {FEATURE_NAMES[i]:<14}  {old[i]:+.3f} -> {mean[i]:+.3f}")
    manifest.setdefault("calib_patches", []).append({
        "via": "apps/diag_recenter_manifest.py",
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "shifted_features": [FEATURE_NAMES[i] for i in idxs],
    })
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dump", type=Path, nargs="?", default=None,
                    help="Live dump JSONL (omit when using --mask-bends only).")
    ap.add_argument("--manifest", type=Path, default=None)
    ap.add_argument("--all-features", action="store_true",
                    help="Shift every feature mean, not just bends/oscs.")
    ap.add_argument("--from-none", action="store_true",
                    help="Use only intended='none' frames (from a structured "
                         "live_record_gestures dump) for the recenter baseline.")
    ap.add_argument("--mask-bends", action="store_true",
                    help="Set feature_std[bend_*] to a huge number so the model "
                         "sees flat zeros for bend dims (bend-blind inference). "
                         "Use this when bend calibration has drifted past saving.")
    args = ap.parse_args()

    mpath = args.manifest or active_manifest()
    print(f"Manifest: {mpath}")
    manifest = load_manifest(mpath)

    if args.mask_bends:
        manifest = _mask_features(manifest, BEND_IDX)

    if args.dump is not None:
        lines = args.dump.read_text(encoding="utf-8").strip().splitlines()
        meta = json.loads(lines[0])["__meta__"]
        records = [json.loads(l) for l in lines[1:]]
        print(f"Live dump: {args.dump.name} "
              f"(label={meta.get('label')!r}, n={len(records)})")
        if Path(meta.get("manifest", "")).name != mpath.name:
            print(f"  [warn] dump was recorded against a different manifest "
                  f"({Path(meta.get('manifest', '')).name}); patches may not "
                  f"transfer cleanly")

        if args.from_none:
            live = _none_means_per_side(records)
            print(f"  baseline source: intended='none' frames only "
                  f"(L={len(live.get('L', []))>0}, R={len(live.get('R', []))>0})")
        else:
            live = _live_means(records)
            print("  baseline source: all live frames (aggregate)")

        idxs = list(range(len(FEATURE_NAMES))) if args.all_features else BEND_OSC_IDX
        manifest = _patch(manifest, live, idxs)
    elif not args.mask_bends:
        ap.error("must pass a dump path or --mask-bends (or both).")

    backup = mpath.with_suffix(mpath.suffix + f".bak_{int(time.time())}")
    shutil.copy2(mpath, backup)
    mpath.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nPatched manifest -> {mpath}")
    print(f"Backup saved      -> {backup}")
    print("Restart collier_live to pick up the new normalization.")


if __name__ == "__main__":
    main()
