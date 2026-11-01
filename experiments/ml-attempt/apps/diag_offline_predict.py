#!/usr/bin/env python3
"""Run the active manifest's model over every clip's *full* timeline and report
how the live pipeline would behave.

Run:  python apps/diag_offline_predict.py [--manifest PATH] [--out ARTIFACT]

What it answers:
* Confusion matrix at the *window* level using the trainer's window/stride.
* Per-clip prediction trace summary: dominant predicted class inside the labeled
  span vs outside it. Reveals "double fire" tendencies (the model lights up the
  wrong class for a few windows before settling).
* Postprocess simulation (debounce + per-class refractory + global cooldown):
  how many commands would fire per clip — should be 1 for a single gesture clip;
  more than 1 indicates a real-world double-fire bug.
* Confidence distribution per (true, predicted) pair: shows whether thresholds
  can rescue precision or just kill recall.

Reads only the saved clips + the manifest's model weights. No glove needed.
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
    Clip,
    active_manifest,
    list_clips,
    load_clip,
    load_manifest,
)
from ml.gesture_tcn import load_model, predict_proba_np


# ---------- Postprocess simulator (mirrors collier_live/postprocess.py) -------


class _PostSim:
    """Per-side: counts how many discrete commands would fire.

    Mirrors collier_live/postprocess.py including hysteresis-based episode
    closing (release_ticks counter).
    """

    def __init__(
        self,
        thresholds: dict[str, float],
        confidence_bump: dict[str, float] | None = None,
        agreement_k: int = 5,
        per_class_refractory_ticks: int = 60,  # 1.0s @60Hz
        global_cooldown_ticks: int = 48,  # 0.8s
        start_margin: float = 0.05,
        end_margin: float = 0.10,
        release_ticks_needed: int = 6,  # 100ms
    ):
        self.thresholds = thresholds
        self.bump = confidence_bump or {}
        self.k = agreement_k
        self.refractory_ticks = per_class_refractory_ticks
        self.global_ticks = global_cooldown_ticks
        self.start_margin = start_margin
        self.end_margin = end_margin
        self.release_ticks_needed = release_ticks_needed
        self.history: list[str] = []
        self.episode_fired: str | None = None
        self.release_ticks = 0
        self.refractory_until: dict[str, int] = {}
        self.global_until = 0
        self.fires: list[tuple[int, str, float]] = []

    def _thr(self, label: str) -> float:
        return self.thresholds.get(label, 0.65) + self.bump.get(label, 0.0)

    def _update_release(self, label: str, conf: float) -> None:
        if self.episode_fired is None:
            self.release_ticks = 0
            return
        end_thr = max(0.0, self._thr(self.episode_fired) - self.end_margin)
        released = (label == "none" or label != self.episode_fired or conf < end_thr)
        if released:
            self.release_ticks += 1
            if self.release_ticks >= self.release_ticks_needed:
                self.episode_fired = None
                self.release_ticks = 0
        else:
            self.release_ticks = 0

    def step(self, tick: int, label: str, conf: float) -> None:
        self.history.append(label)
        self._update_release(label, conf)
        if label == "none":
            return
        if len(self.history) < self.k:
            return
        if not all(h == label for h in self.history[-self.k:]):
            return
        if self.episode_fired is not None:
            return
        if tick < self.global_until:
            return
        if tick < self.refractory_until.get(label, 0):
            return
        start_thr = self._thr(label) + self.start_margin
        if conf < start_thr:
            return
        self.fires.append((tick, label, conf))
        self.episode_fired = label
        self.release_ticks = 0
        self.refractory_until[label] = tick + self.refractory_ticks
        self.global_until = tick + self.global_ticks


# ---------- Window / prediction utilities ------------------------------------


def _rolling_predict(
    x: np.ndarray, mean: np.ndarray, std: np.ndarray, window: int, model
) -> tuple[np.ndarray, np.ndarray]:
    """Return per-tick (label_idx, max_prob, full_probs[T,C]).
    For ticks before `window-1` returns -1 / 0.0 / zeros.
    """
    n = x.shape[0]
    norm = (x - mean) / std
    probs_seq = np.zeros((n, 0), dtype=np.float32)
    if n < window:
        return np.full(n, -1, np.int64), np.zeros((n, 0), np.float32)
    batch = np.stack(
        [norm[i:i + window] for i in range(n - window + 1)], axis=0
    ).astype(np.float32)
    # Predict in batches to avoid mps OOM on small machines.
    out = []
    B = 256
    for i in range(0, batch.shape[0], B):
        out.append(predict_proba_np(model, batch[i:i + B]))
    full = np.concatenate(out, axis=0)
    probs_seq = np.zeros((n, full.shape[1]), dtype=np.float32)
    probs_seq[window - 1:] = full
    return probs_seq.argmax(axis=1), probs_seq


def _label_per_tick(clip: Clip, side: str, window: int, none_idx: int,
                    class_index: dict[str, int]) -> np.ndarray:
    """Per-tick true label using same overlap-fraction rule as the trainer.

    Each *tick* corresponds to a window that ENDS at that tick (live semantic).
    Window covers [t-window+1, t+1).
    """
    n = clip.x(side).shape[0]
    label = clip.label(side)
    s, e = clip.span(side)
    out = np.full(n, -1, np.int64)
    for t in range(window - 1, n):
        ws, we = t - window + 1, t + 1
        if label == "none":
            out[t] = none_idx
            continue
        ov = max(0, min(we, e) - max(ws, s))
        wlen = we - ws
        frac = ov / wlen
        if frac >= 0.6:
            out[t] = class_index.get(label, none_idx)
        elif ov == 0:
            out[t] = none_idx
        else:
            out[t] = -1  # ambiguous boundary
    return out


# ---------- Reports ----------------------------------------------------------


def _confusion(labels: list[str], y_true: np.ndarray, y_pred: np.ndarray) -> str:
    n = len(labels)
    mat = np.zeros((n, n), dtype=np.int64)
    mask = y_true >= 0
    for t, p in zip(y_true[mask], y_pred[mask]):
        if 0 <= p < n and 0 <= t < n:
            mat[t, p] += 1
    name_w = max(len(s) for s in labels) + 1
    lines = []
    head = " " * name_w + "  ".join(f"{labels[j][:6]:>6}" for j in range(n))
    lines.append(head)
    for i, name in enumerate(labels):
        total = mat[i].sum()
        cells = []
        for j in range(n):
            v = mat[i, j]
            cell = f"{v:>6}"
            if v and i != j and v > total * 0.05:
                cell = f"\x1b[31m{cell}\x1b[0m"
            if i == j and v:
                cell = f"\x1b[32m{cell}\x1b[0m"
            cells.append(cell)
        recall = (mat[i, i] / total) if total else 0.0
        lines.append(f"{name:<{name_w}}" + "  ".join(cells) + f"   sup={total} R={recall:.2f}")
    return "\n".join(lines)


def _topk(probs: np.ndarray, names: list[str], k: int = 3) -> str:
    idx = np.argsort(-probs)[:k]
    return " ".join(f"{names[i]}:{probs[i]:.2f}" for i in idx)


def run(manifest_path: Path, out: Path | None) -> None:
    manifest = load_manifest(manifest_path)
    class_names: list[str] = list(manifest["class_names"])
    window: int = int(manifest["window_size"])
    class_index = {n: i for i, n in enumerate(class_names)}
    none_idx = class_index.get("none", 0)
    base_dir = manifest_path.parent
    sides_data = {}
    for side in ("L", "R"):
        sd = manifest["sides"][side]
        weights = Path(sd["model_weights_path"])
        if not weights.is_absolute():
            weights = base_dir / weights.name
        sides_data[side] = {
            "model": load_model(weights),
            "mean": np.asarray(sd["feature_mean"], dtype=np.float32),
            "std": np.asarray(sd["feature_std"], dtype=np.float32),
            "thresholds": {str(k): float(v) for k, v in sd.get("class_thresholds", {}).items()},
        }

    clips = [load_clip(p) for p in list_clips()]

    lines: list[str] = []
    lines.append(f"Manifest: {manifest_path.name}")
    lines.append(f"Classes: {class_names}")
    lines.append(f"Window: {window} frames (~{window/60:.2f}s @60Hz)\n")

    # Per-side aggregate confusion + per-clip summary
    for side in ("L", "R"):
        data = sides_data[side]
        all_true: list[np.ndarray] = []
        all_pred: list[np.ndarray] = []
        per_class_summary: dict[str, dict] = defaultdict(lambda: {
            "n_clips": 0,
            "in_span_correct": 0,
            "in_span_total": 0,
            "in_span_pred_hist": Counter(),
            "leading_pred_hist": Counter(),  # before span begins (label=none)
            "trailing_pred_hist": Counter(),  # after span ends (label=none)
            "fire_count": Counter(),  # n_commands -> n_clips
            "double_fire_examples": [],
        })

        for c in clips:
            x = c.x(side)
            if x.size == 0 or x.shape[0] < window:
                continue
            true_lbl = c.label(side)
            pred_idx, probs_seq = _rolling_predict(
                x, data["mean"], data["std"], window, data["model"]
            )
            y_true = _label_per_tick(c, side, window, none_idx, class_index)
            all_true.append(y_true)
            all_pred.append(pred_idx)

            cs = per_class_summary[true_lbl]
            cs["n_clips"] += 1

            # In-span breakdown
            s, e = c.span(side)
            in_span_mask = (np.arange(x.shape[0]) >= max(s, window - 1)) & (np.arange(x.shape[0]) < e)
            pre_mask = np.arange(x.shape[0]) < s
            post_mask = np.arange(x.shape[0]) >= e
            valid = pred_idx >= 0

            for t in np.where(in_span_mask & valid)[0]:
                pname = class_names[pred_idx[t]]
                cs["in_span_pred_hist"][pname] += 1
                cs["in_span_total"] += 1
                if true_lbl != "none" and pname == true_lbl:
                    cs["in_span_correct"] += 1
            for t in np.where(pre_mask & valid)[0]:
                cs["leading_pred_hist"][class_names[pred_idx[t]]] += 1
            for t in np.where(post_mask & valid)[0]:
                cs["trailing_pred_hist"][class_names[pred_idx[t]]] += 1

            sim = _PostSim(
                data["thresholds"],
                confidence_bump={"semitone_up": 0.10, "semitone_down": 0.10},
            )
            for t in range(x.shape[0]):
                if pred_idx[t] < 0:
                    continue
                lbl = class_names[pred_idx[t]]
                conf = float(probs_seq[t, pred_idx[t]])
                sim.step(t, lbl, conf)
            cs["fire_count"][len(sim.fires)] += 1
            if true_lbl != "none" and len(sim.fires) >= 2:
                fired_labels = [f[1] for f in sim.fires]
                if len(cs["double_fire_examples"]) < 5:
                    cs["double_fire_examples"].append((c.path.name, fired_labels))

        y_true_all = np.concatenate(all_true) if all_true else np.array([], np.int64)
        y_pred_all = np.concatenate(all_pred) if all_pred else np.array([], np.int64)

        lines.append(f"\n========== Side {side} ==========")
        lines.append(f"Total ticks (valid labels): {(y_true_all >= 0).sum()}")
        lines.append("Per-window confusion (raw argmax, true rows / pred cols):")
        lines.append(_confusion(class_names, y_true_all, y_pred_all))

        lines.append("\nPer-true-class behavior:")
        for true_lbl in sorted(per_class_summary):
            cs = per_class_summary[true_lbl]
            if cs["in_span_total"] == 0 and true_lbl != "none":
                continue
            top_in = ", ".join(f"{k}:{v}" for k, v in cs["in_span_pred_hist"].most_common(4))
            top_pre = ", ".join(f"{k}:{v}" for k, v in cs["leading_pred_hist"].most_common(3))
            top_post = ", ".join(f"{k}:{v}" for k, v in cs["trailing_pred_hist"].most_common(3))
            acc = cs["in_span_correct"] / max(cs["in_span_total"], 1) if true_lbl != "none" else 0.0
            fires = sorted(cs["fire_count"].items())
            fires_str = " ".join(f"{n}x:{m}" for n, m in fires)
            lines.append(f"\n  {true_lbl}  ({cs['n_clips']} clips)")
            if true_lbl != "none":
                lines.append(f"    in-span argmax acc = {acc:.2f}")
            lines.append(f"    in-span preds:  {top_in}")
            if top_pre:
                lines.append(f"    pre-span preds: {top_pre}")
            if top_post:
                lines.append(f"    post-span:      {top_post}")
            lines.append(f"    postproc fires per clip: {fires_str}")
            for name, fired in cs["double_fire_examples"]:
                lines.append(f"      DOUBLE: {name} -> {fired}")

    report = "\n".join(lines)
    print(report)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report + "\n", encoding="utf-8")
        print(f"\nWrote report -> {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=Path("artifacts/reports/diag_offline_predict.txt"))
    args = ap.parse_args()
    mpath = args.manifest or active_manifest()
    run(mpath, args.out)


if __name__ == "__main__":
    main()
