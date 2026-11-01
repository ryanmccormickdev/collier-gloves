#!/usr/bin/env python3
"""Frame-by-frame view of one clip: signal trace + model predictions +
postprocess decisions.

Run:  python apps/diag_clip_view.py CLIP_NAME [--side L|R|both] [--ascii-plot]

CLIP_NAME may be a substring; the first matching clip is used. The marked
label span is highlighted. Use this to investigate any clip flagged as a
double-fire by diag_offline_predict.py.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps._diag_shared import (
    FEATURE_NAMES,
    FIDX,
    Clip,
    active_manifest,
    list_clips,
    load_clip,
    load_manifest,
)
from ml.gesture_tcn import load_model, predict_proba_np


def _find_clip(needle: str) -> Path:
    paths = list_clips()
    matches = [p for p in paths if needle in p.name]
    if not matches:
        raise SystemExit(f"No clip matches {needle!r} in {paths[0].parent}")
    if len(matches) > 1:
        print(f"# {len(matches)} matches; using first: {matches[0].name}")
    return matches[0]


def _normalize(x: np.ndarray, mean: np.ndarray, std: np.ndarray) -> np.ndarray:
    return (x - mean) / std


def _predict_rolling(model, x_norm: np.ndarray, window: int) -> np.ndarray:
    n = x_norm.shape[0]
    if n < window:
        return np.zeros((n, 0), dtype=np.float32)
    batch = np.stack(
        [x_norm[i:i + window] for i in range(n - window + 1)], axis=0
    ).astype(np.float32)
    out = []
    for i in range(0, batch.shape[0], 256):
        out.append(predict_proba_np(model, batch[i:i + 256]))
    full = np.concatenate(out, axis=0)
    probs = np.zeros((n, full.shape[1]), dtype=np.float32)
    probs[window - 1:] = full
    return probs


def _ascii_bar(v: float, lo: float, hi: float, width: int = 24) -> str:
    if hi <= lo:
        return " " * width
    f = max(0.0, min(1.0, (v - lo) / (hi - lo)))
    n = int(round(f * width))
    return "#" * n + "." * (width - n)


def _annotate_span(t: int, start: int, end: int) -> str:
    if t < start:
        return " "
    if t < end:
        return "█"
    return "·"


def _print_side(
    side: str,
    clip: Clip,
    manifest: dict,
    model,
    mean: np.ndarray,
    std: np.ndarray,
    thresholds: dict[str, float],
    ascii_plot: bool,
) -> None:
    class_names: list[str] = list(manifest["class_names"])
    window: int = int(manifest["window_size"])
    x = clip.x(side)
    if x.shape[0] < window:
        print(f"[{side}] clip too short ({x.shape[0]} < window {window})")
        return
    x_norm = _normalize(x, mean, std)
    probs = _predict_rolling(model, x_norm, window)
    pred_idx = probs.argmax(axis=1)
    pred_conf = probs.max(axis=1)
    label = clip.label(side)
    s, e = clip.span(side)

    print(f"\n========== [{side}] true={label}  span=[{s},{e})  n={x.shape[0]} ==========")
    # Header
    cols = ("t", "span", "pred", "conf",
            "wrist_tilt", "palm_roll", "fwd_up", "open", "vert_vel", "v_along", "v_across",
            "bend_idx", "bend_mid", "bend_pky", "osc_idx", "osc_mid")
    feat_keys = ("wrist_tilt", "palm_roll", "forward_up", "openness", "vert_vel",
                 "vel_along", "vel_across", "bend_index", "bend_middle", "bend_pinky",
                 "osc_index", "osc_middle")
    print(f"  {'t':>4} {'  span':6} {'pred':<23} {'conf':>5}  " +
          " ".join(f"{k:>8}" for k in feat_keys))
    # Decimate so the output isn't gigantic.
    step = max(1, x.shape[0] // 80)
    for t in range(0, x.shape[0], step):
        marker = _annotate_span(t, s, e)
        if t < window - 1:
            pname, conf = "(warmup)", 0.0
        else:
            pidx = int(pred_idx[t])
            pname = class_names[pidx]
            conf = float(pred_conf[t])
            thr = thresholds.get(pname, 0.65)
            gated = pname if (pname == "none" or conf >= thr) else f"none(<{pname})"
            pname = gated
        feats = " ".join(f"{x[t, FIDX[k]]:+8.2f}" for k in feat_keys)
        print(f"  {t:>4}  {marker*4} {pname:<23} {conf:>5.2f}  {feats}")

    # Top-3 probability trace at every Nth tick
    if probs.shape[1] > 0:
        print("\n  -- top-3 per-tick probabilities --")
        for t in range(window - 1, x.shape[0], step):
            top = np.argsort(-probs[t])[:3]
            marker = _annotate_span(t, s, e)
            parts = " ".join(f"{class_names[i]}:{probs[t,i]:.2f}" for i in top)
            print(f"    {t:>4} {marker} {parts}")

    # Postprocess sim summary
    fires: list[tuple[int, str, float]] = []
    refractory_until: dict[str, int] = {}
    global_until = 0
    episode_fired: str | None = None
    history: list[str] = []
    K = 2
    refractory_ticks = 27
    global_ticks = 9
    for t in range(x.shape[0]):
        if t < window - 1:
            history.append("none")
            continue
        pname = class_names[int(pred_idx[t])]
        history.append(pname)
        if pname == "none":
            episode_fired = None
            continue
        if len(history) < K or not all(h == pname for h in history[-K:]):
            continue
        if episode_fired == pname:
            continue
        if t < global_until or t < refractory_until.get(pname, 0):
            continue
        conf = float(probs[t, int(pred_idx[t])])
        thr = thresholds.get(pname, 0.65)
        if conf < thr:
            continue
        fires.append((t, pname, conf))
        episode_fired = pname
        refractory_until[pname] = t + refractory_ticks
        global_until = t + global_ticks
    print(f"\n  Postprocess would emit {len(fires)} command(s):")
    for t, lbl, conf in fires:
        print(f"    t={t:>4}  {lbl}  conf={conf:.2f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("needle", type=str, help="Substring of clip filename")
    ap.add_argument("--side", choices=("L", "R", "both"), default="both")
    ap.add_argument("--manifest", type=Path, default=None)
    ap.add_argument("--ascii-plot", action="store_true")
    args = ap.parse_args()
    path = _find_clip(args.needle)
    clip = load_clip(path)
    mpath = args.manifest or active_manifest()
    manifest = load_manifest(mpath)
    base_dir = mpath.parent
    print(f"Manifest: {mpath.name}  arch={manifest.get('model_arch')}")
    print(f"Clip: {path.name}")
    sides = ("L", "R") if args.side == "both" else (args.side,)
    for side in sides:
        sd = manifest["sides"][side]
        weights = Path(sd["model_weights_path"])
        if not weights.is_absolute():
            weights = base_dir / weights.name
        model = load_model(weights)
        mean = np.asarray(sd["feature_mean"], dtype=np.float32)
        std = np.asarray(sd["feature_std"], dtype=np.float32)
        thresholds = {str(k): float(v) for k, v in sd.get("class_thresholds", {}).items()}
        _print_side(side, clip, manifest, model, mean, std, thresholds, args.ascii_plot)


if __name__ == "__main__":
    main()
