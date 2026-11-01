#!/usr/bin/env python3
"""Retrain the per-hand gesture TCN with the curated defaults from
``docs/diagnosis_findings.md`` and write a full manifest. Optionally point
``collier_live/models/active.json`` at the new manifest.

Run:
  # Conservative (slim vocab, no augment, window 96):
  python apps/diag_retrain.py --activate

  # Wider vocab including chromatic:
  python apps/diag_retrain.py --classes \
    spawn_note,scale_up,scale_down,semitone_up,semitone_down,\
collective_scale_up,collective_scale_down,collective_semitone_up,collective_semitone_down \
    --activate

  # Inherit flex_range from a previous calibrated manifest:
  python apps/diag_retrain.py --flex-from \
    gesture_trainer_data/models/gesture_tcn_20260602_001602.manifest.json

This script reuses ``glove_gesture_trainer`` but provides better defaults:
* window 96 frames (~1.6 s) so the whole gesture lives in one window.
* No augmentation by default (user reports no-augment models work best).
* Drops clips whose recorded class isn't in the active vocab (fixed in
  ``build_windowed_dataset``) so ``none`` isn't polluted by e.g. volume
  gestures.
* Defaults to the v2 dilated arch but allows reverting to v1 if you'd like.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ml.augment import AugmentConfig
from ml.gesture_tcn import (
    GestureTCNConfig,
    MODEL_ARCH_V1,
    MODEL_ARCH_V2,
    predict_proba_np,
    save_model as save_gesture_model,
    train_gesture_model,
)

import glove_gesture_trainer as ggt

DEFAULT_SLIM = (
    "spawn_note,scale_up,scale_down,collective_scale_up,collective_scale_down",
)


def _choose_thresholds(probs: np.ndarray, y: np.ndarray, names: list[str]) -> dict:
    out: dict[str, float] = {}
    for i, name in enumerate(names):
        if name == "none":
            out[name] = 1.0
            continue
        mask_pos = y == i
        mask_neg = ~mask_pos
        if mask_pos.sum() == 0:
            out[name] = 0.65
            continue
        best_thr, best_f1 = 0.5, 0.0
        for thr in np.linspace(0.4, 0.95, 12):
            pred = (probs[:, i] >= thr) & (probs.argmax(axis=1) == i)
            tp = int((pred & mask_pos).sum())
            fp = int((pred & mask_neg).sum())
            fn = int((~pred & mask_pos).sum())
            if tp == 0:
                continue
            p = tp / (tp + fp)
            r = tp / (tp + fn)
            f1 = 2 * p * r / (p + r) if (p + r) else 0.0
            if f1 > best_f1:
                best_f1, best_thr = f1, float(thr)
        out[name] = best_thr
    return out


def _evaluate(probs: np.ndarray, y: np.ndarray, names: list[str], thresholds: dict) -> dict:
    pred = probs.argmax(axis=1)
    out = {"per_class_precision": {}, "per_class_recall": {}, "per_class_f1": {}}
    for i, name in enumerate(names):
        thr = thresholds.get(name, 0.5)
        # "none" never gates by threshold (it's the fallback class), so score
        # raw argmax for it.
        if name == "none":
            gated = pred == i
        else:
            gated = (pred == i) & (probs[:, i] >= thr)
        tp = int(((y == i) & gated).sum())
        fp = int(((y != i) & gated).sum())
        fn = int(((y == i) & ~gated).sum())
        p = tp / (tp + fp) if (tp + fp) else 0.0
        r = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * p * r / (p + r) if (p + r) else 0.0
        out["per_class_precision"][name] = p
        out["per_class_recall"][name] = r
        out["per_class_f1"][name] = f1
    out["accuracy"] = float((pred == y).mean())
    out["false_positive_rate"] = float(
        ((pred != 0) & (y == 0)).sum() / max(int((y == 0).sum()), 1)
    )
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--classes", type=str, default=DEFAULT_SLIM[0],
                    help="Comma-separated active vocab (excluding 'none', which is always included).")
    # Gesture spans in the existing dataset average ~50-60 frames (~1s); a
    # larger window than ~72 drops most of the gesture clips through the
    # 60% overlap rule. Keep 64 unless you re-record longer gestures.
    ap.add_argument("--window", type=int, default=64)
    ap.add_argument("--stride", type=int, default=8)
    ap.add_argument("--epochs", type=int, default=45)
    ap.add_argument("--channels", type=int, default=64)
    ap.add_argument("--arch", choices=(MODEL_ARCH_V1, MODEL_ARCH_V2), default=MODEL_ARCH_V2)
    ap.add_argument("--augment-time-warp", action="store_true",
                    help="Enable the only augmentation worth keeping (±10%% time warp).")
    ap.add_argument("--flex-from", type=Path, default=None,
                    help="Inherit flex_range from another manifest.")
    ap.add_argument("--activate", action="store_true",
                    help="Update collier_live/models/active.json to this new model.")
    args = ap.parse_args()

    picked = {s.strip() for s in args.classes.split(",") if s.strip() and s.strip() != "none"}
    active = ggt.resolve_active_vocab(picked)
    n_classes = len(active)
    print(f"Active vocab: {active}")
    print(f"Window={args.window}  stride={args.stride}  arch={args.arch}  epochs={args.epochs}")

    aug = AugmentConfig(time_warp=args.augment_time_warp)

    flex_range: dict = {}
    if args.flex_from is not None:
        prev = json.loads(args.flex_from.read_text(encoding="utf-8"))
        flex_range = prev.get("flex_range") or {}
        print(f"Inheriting flex_range from {args.flex_from.name}: "
              f"L={'yes' if 'L' in flex_range else 'no'} "
              f"R={'yes' if 'R' in flex_range else 'no'}")

    model_id = f"gesture_tcn_{time.strftime('%Y%m%d_%H%M%S')}"
    sides_manifest: dict = {}
    for side in ("L", "R"):
        print(f"\n[{side}] building windowed dataset…")
        x, y, groups, skipped = ggt.build_windowed_dataset(
            side, args.window, args.stride,
            active_vocab=active, excluded_as_none=False,
        )
        print(f"  {x.shape[0]} windows from {len(np.unique(groups))} clips ({skipped} skipped)")
        from collections import Counter
        cnt = Counter(active[i] for i in y)
        for name in active:
            print(f"    {name:26} {cnt.get(name, 0):5}")

        train_idx, val_idx = ggt.split_by_group(groups, y)
        flat = x[train_idx].reshape(-1, ggt.N_FEATURES)
        mean = flat.mean(axis=0)
        std = flat.std(axis=0) + 1e-6
        x = ((x - mean) / std).astype(np.float32)

        cfg = GestureTCNConfig(epochs=args.epochs, channels=args.channels)
        def progress(e, total, _s=side):
            if e == total or e % 5 == 0:
                print(f"    [{_s}] epoch {e}/{total}")
        fit = train_gesture_model(
            ggt.N_FEATURES, n_classes, cfg,
            x[train_idx], y[train_idx],
            x_val=x[val_idx] if len(val_idx) else None,
            y_val=y[val_idx] if len(val_idx) else None,
            progress=progress, augment=aug, model_arch=args.arch,
        )
        model = fit["model"]
        print(f"  train_acc={fit['train_acc']:.3f}  val_acc={fit.get('val_acc', float('nan')):.3f}")

        eval_idx = val_idx if len(val_idx) else train_idx
        probs = predict_proba_np(model, x[eval_idx])
        thresholds = _choose_thresholds(probs, y[eval_idx], list(active))
        report = _evaluate(probs, y[eval_idx], list(active), thresholds)
        print("  thresholds:", {k: round(v, 2) for k, v in thresholds.items()})
        print(f"  per-class F1: " + " ".join(
            f"{name[:8]}:{report['per_class_f1'].get(name, 0):.2f}"
            for name in active
        ))

        weights_path = ggt.MODELS_DIR / f"{model_id}_{side}.pt"
        save_gesture_model(model, weights_path,
                           in_features=ggt.N_FEATURES, n_classes=n_classes,
                           model_arch=args.arch)
        sides_manifest[side] = {
            "model_weights_path": str(weights_path),
            "feature_mean": mean.astype(float).tolist(),
            "feature_std": std.astype(float).tolist(),
            "class_thresholds": thresholds,
            "metrics": {
                "train_acc": fit["train_acc"],
                "val_acc": fit.get("val_acc"),
                "device": fit.get("device"),
                "test_acc": report["accuracy"],
                "false_positive_rate": report["false_positive_rate"],
            },
            "per_class_precision": report["per_class_precision"],
            "per_class_recall": report["per_class_recall"],
            "per_class_f1": report["per_class_f1"],
        }

    manifest = {
        "model_id": model_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "vocab_version": ggt.VOCAB_VERSION,
        "feature_version": ggt.FEATURE_VERSION,
        "model_arch": args.arch,
        "class_names": list(active),
        "window_size": args.window,
        "stride": args.stride,
        "feature_names": list(ggt.FEATURE_NAMES),
        "flex_range": flex_range,
        "train_config": {
            "full_vocab": list(ggt.VOCAB),
            "active_classes": list(active),
            "excluded_as_none": False,
            "augment": aug.to_dict(),
            "via": "apps/diag_retrain.py",
        },
        "sides": sides_manifest,
    }
    manifest_path = ggt.MODELS_DIR / f"{model_id}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nWrote {manifest_path}")

    if args.activate:
        active_json = ROOT / "collier_live" / "models" / "active.json"
        active_json.parent.mkdir(parents=True, exist_ok=True)
        active_json.write_text(json.dumps({
            "manifest_path": str(manifest_path.relative_to(ROOT)),
        }, indent=2) + "\n", encoding="utf-8")
        print(f"Updated {active_json} -> {manifest_path.name}")


if __name__ == "__main__":
    main()
