#!/usr/bin/env python3
"""Diagnose gesture clips + PyTorch TCN without the Qt trainer UI.

Examples:
  python scripts/gesture_train_diagnose.py --side L
  python scripts/gesture_train_diagnose.py --side L --classes none,spawn_note --epochs 30
  python scripts/gesture_train_diagnose.py --side L --overfit 64 --epochs 80
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ml.augment import AugmentConfig
from ml.gesture_tcn import (
    GestureTCNConfig,
    MODEL_ARCH,
    predict_proba_np,
    train_gesture_model,
)
import glove_gesture_trainer as ggt


def _metrics(y_true: np.ndarray, y_pred: np.ndarray, names: list[str]) -> dict:
    out = {}
    for i, name in enumerate(names):
        tp = int(((y_true == i) & (y_pred == i)).sum())
        fp = int(((y_true != i) & (y_pred == i)).sum())
        fn = int(((y_true == i) & (y_pred != i)).sum())
        p = tp / (tp + fp) if (tp + fp) else 0.0
        r = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * p * r / (p + r) if (p + r) else 0.0
        out[name] = {"P": p, "R": r, "F1": f1, "support": int((y_true == i).sum())}
    neg = y_true == 0
    fp_none = int(((y_true == 0) & (y_pred != 0)).sum())
    out["_acc"] = float((y_pred == y_true).mean()) if len(y_true) else 0.0
    out["_fp_rate_none"] = fp_none / max(int(neg.sum()), 1)
    return out


def _print_counts(side: str, y: np.ndarray, skipped: int, active: tuple[str, ...]) -> None:
    c = Counter(active[i] for i in y)
    print(f"\n=== {side} windows={len(y)} skipped_clips={skipped} ===")
    for name in active:
        print(f"  {name:26} {c.get(name, 0):4}")


def _print_report(title: str, m: dict) -> None:
    print(f"\n--- {title} ---")
    print(f"  accuracy = {m['_acc']:.3f}   none_fp_rate = {m['_fp_rate_none']:.3f}")
    for name, d in m.items():
        if name.startswith("_"):
            continue
        if d["support"] == 0 and d["R"] == 0 and d["P"] == 0:
            continue
        print(f"  {name:26} sup={d['support']:4}  P={d['P']:.3f} R={d['R']:.3f} F1={d['F1']:.3f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--side", choices=("L", "R"), default="L")
    ap.add_argument("--window", type=int, default=64)
    ap.add_argument("--stride", type=int, default=12)
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--channels", type=int, default=64)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument(
        "--classes",
        type=str,
        default="",
        help="Comma-separated subset of VOCAB (e.g. none,spawn_note). Empty = all.",
    )
    ap.add_argument(
        "--overfit",
        type=int,
        default=0,
        help="If >0, train on this many random windows only (sanity check).",
    )
    ap.add_argument("--counts-only", action="store_true")
    args = ap.parse_args()

    if args.classes.strip():
        picked = {s.strip() for s in args.classes.split(",") if s.strip() and s.strip() != "none"}
        active = ggt.resolve_active_vocab(picked)
    else:
        active = ggt.VOCAB

    x, y, groups, skipped = ggt.build_windowed_dataset(
        args.side, args.window, args.stride,
        active_vocab=active, excluded_as_none=True,
    )
    _print_counts(args.side, y, skipped, active)

    if args.counts_only:
        return

    train_idx, val_idx = ggt.split_by_group(groups, y)
    flat = x[train_idx].reshape(-1, ggt.N_FEATURES)
    mean = flat.mean(axis=0)
    std = flat.std(axis=0) + 1e-6
    x = ((x - mean) / std).astype(np.float32)

    if args.overfit > 0:
        n = min(args.overfit, len(train_idx))
        rng = np.random.default_rng(0)
        train_idx = rng.choice(train_idx, n, replace=False)
        val_idx = train_idx[: max(1, n // 5)]
        print(f"\nOverfit mode: train={len(train_idx)} val={len(val_idx)} windows")

    cfg = GestureTCNConfig(
        epochs=args.epochs,
        channels=args.channels,
        batch_size=args.batch_size,
        learning_rate=args.lr,
    )
    print(f"\nTraining {MODEL_ARCH} ({cfg.epochs} epochs, {cfg.channels} ch) …")
    class_names = list(active)
    result = train_gesture_model(
        ggt.N_FEATURES,
        len(active),
        cfg,
        x[train_idx],
        y[train_idx],
        x_val=x[val_idx] if len(val_idx) else None,
        y_val=y[val_idx] if len(val_idx) else None,
        model_arch=MODEL_ARCH,
    )
    model = result["model"]
    print(f"device={result['device']}  train_acc={result['train_acc']:.3f}  "
          f"val_acc={result.get('val_acc', float('nan')):.3f}")

    eval_idx = val_idx if len(val_idx) else train_idx
    probs = predict_proba_np(model, x[eval_idx])
    raw_pred = probs.argmax(axis=1)
    _print_report("raw argmax (no confidence gate)", _metrics(y[eval_idx], raw_pred, class_names))

    thresholds = ggt.choose_thresholds(probs, y[eval_idx], class_names)
    gated_pred = probs.argmax(axis=1).copy()
    for i, cls in enumerate(gated_pred):
        name = class_names[int(cls)]
        if name != "none" and float(probs[i, cls]) < thresholds.get(name, 0.65):
            gated_pred[i] = 0
    gm = _metrics(y[eval_idx], gated_pred, class_names)
    _print_report("with per-class thresholds (trainer UI)", gm)

    logits = model(torch.as_tensor(x[eval_idx], dtype=torch.float32)).detach().numpy()
    mean_logits = logits.mean(axis=0)
    print("\nMean logit per class (val):")
    for i, name in enumerate(class_names):
        print(f"  {name:26} {mean_logits[i]:+.3f}")


if __name__ == "__main__":
    main()
