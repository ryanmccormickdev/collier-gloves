#!/usr/bin/env python3
"""Interactive per-gesture live capture for diagnosis. Prompts you for each
gesture, records a fixed window while you perform it, and writes one JSONL
file where every tick is tagged with the gesture you were asked to perform.

This is the proper companion to ``apps/diag_live_vs_clips.py`` — instead of
aggregating a mixed 30-second probe (which mostly contained 'none' ticks and
muddied the comparison), every tick comes with a known intended class so you
can compare *like for like* against the training distribution.

Run:
  python apps/diag_live_record_gestures.py \\
    --classes none,scale_up,scale_down,semitone_up,semitone_down,\
collective_scale_up,collective_scale_down \\
    --secs 4

Workflow per gesture:
  1. Get-ready countdown (3..2..1).
  2. PERFORM-NOW window (default 4 s). Repeat the gesture cleanly the whole time.
  3. Brief pause, then next gesture.

After the run, ``apps/diag_live_compare_gestures.py`` (or just looking at
the JSONL) shows feature deltas per class vs the same training class. This
is what you want for "why does the model think my scale_up is something
else".
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collier.config import Config
from collier.frames import RawFrame
from collier.transport import GloveTransport
from glove_ml_core.features import sig_features
from glove_ml_core.flex_live import SessionFlexTracker
from glove_ml_core.signals import GloveConditioner as V3Conditioner

from apps._diag_shared import active_manifest, load_manifest
from ml.gesture_tcn import load_model, predict_proba_np


DEFAULT_CLASSES = (
    "none,scale_up,scale_down,semitone_up,semitone_down,"
    "collective_scale_up,collective_scale_down"
)


class GestureRecorder:
    def __init__(self, manifest_path: Path, secs: float, classes: list[str],
                 out_dir: Path, calibrate_secs: float):
        self.cfg = Config()
        self.manifest_path = manifest_path
        self.manifest = load_manifest(manifest_path)
        self.class_names = list(self.manifest["class_names"])
        self.window = int(self.manifest["window_size"])
        self.secs = secs
        self.calibrate_secs = calibrate_secs
        self.classes = classes

        base_dir = manifest_path.parent
        self.sides = {}
        for side in ("L", "R"):
            sd = self.manifest["sides"][side]
            weights = Path(sd["model_weights_path"])
            if not weights.is_absolute():
                weights = base_dir / weights.name
            self.sides[side] = {
                "model": load_model(weights),
                "mean": np.asarray(sd["feature_mean"], dtype=np.float32),
                "std": np.asarray(sd["feature_std"], dtype=np.float32),
                "frames": [],
            }

        n_ch = self.cfg.packet.flex_count
        self.cond = {"L": V3Conditioner("L"), "R": V3Conditioner("R")}
        self.flex_tracker = {
            "L": SessionFlexTracker(n_ch, min_span=80.0, decay_per_s=0.0),
            "R": SessionFlexTracker(n_ch, min_span=80.0, decay_per_s=0.0),
        }
        # If the manifest already has a flex_range, apply it.
        fr = self.manifest.get("flex_range") or {}
        for side in ("L", "R"):
            sd = fr.get(side) or fr.get(side.lower())
            if sd and "lo" in sd and "hi" in sd:
                self.cond[side].set_flex_range(sd["lo"], sd["hi"])

        self._latest: dict[str, RawFrame | None] = {"L": None, "R": None}
        self._transport = GloveTransport(self.cfg.ble, self.cfg.packet, self._on_frame)

        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.out_path = out_dir / f"live_record_{stamp}.jsonl"
        self.records: list[dict] = []

    def _on_frame(self, frame: RawFrame) -> None:
        self._latest[frame.side] = frame

    def _fresh(self, side: str, t: float) -> RawFrame | None:
        f = self._latest[side]
        if f is None or t - f.recv_t > self.cfg.process.stale_after_s:
            return None
        return f

    async def run(self) -> None:
        await asyncio.gather(self._transport.run(), self._main())

    async def _main(self) -> None:
        print(f"\nMANIFEST: {self.manifest_path.name}")
        print(f"OUT:      {self.out_path}\n")
        print("Waiting for both gloves to connect…")
        while self._latest["L"] is None or self._latest["R"] is None:
            await asyncio.sleep(0.1)
        print("Both gloves present.\n")

        # Calibration warmup so the rolling envelope has a reasonable range
        # before we trust any predictions.
        if self.calibrate_secs > 0 and not self.cond["L"].has_flex_range:
            print(f"CALIBRATION ({self.calibrate_secs:.0f}s): WIGGLE EVERY FINGER OF BOTH HANDS FULLY")
            print("  (full extend <-> full curl, each finger, several times)")
            await self._record_block("__calibration__", self.calibrate_secs)
            print("Calibration done.\n")

        for i, cls in enumerate(self.classes, 1):
            await self._countdown(cls, i, len(self.classes))
            await self._record_block(cls, self.secs)
            print()
            await asyncio.sleep(0.6)

        self._save()
        print(f"\nDone. {len(self.records)} ticks recorded.")

    async def _countdown(self, cls: str, i: int, total: int) -> None:
        print(f"=== ({i}/{total}) NEXT GESTURE: {cls!r} ===")
        if cls == "none":
            print("  Hold both hands in a neutral, RESTING pose.")
        elif cls.startswith("collective_"):
            print(f"  Perform {cls!r} on BOTH HANDS together (palm-rolled position).")
        else:
            base = cls.replace("_up", "").replace("_down", "")
            print(f"  Perform {cls!r} REPEATEDLY for the next {self.secs:.0f}s.")
            print(f"  Reset to neutral briefly between reps.")
        for n in (3, 2, 1):
            print(f"  starting in {n}…")
            await asyncio.sleep(0.7)

    async def _record_block(self, cls: str, secs: float) -> None:
        print(f"  ● RECORDING {cls!r} for {secs:.1f}s")
        dt = self.cfg.process.dt
        t0 = time.monotonic()
        # Reset per-block buffers — model predictions during this block use a
        # rolling window seeded from t0, so don't mix gesture boundaries.
        for side in ("L", "R"):
            self.sides[side]["frames"] = []
        last_print = 0.0
        while True:
            now = time.monotonic()
            elapsed = now - t0
            if elapsed >= secs:
                break
            rec: dict = {"t": now, "elapsed": elapsed, "intended": cls}
            for side in ("L", "R"):
                raw = self._fresh(side, now)
                if raw is not None and not self.cond[side].has_flex_range:
                    flex = self.flex_tracker[side].update(tuple(raw.flex), dt)
                    if flex:
                        self.cond[side].set_flex_range(flex["lo"], flex["hi"])
                sig = self.cond[side].process(raw, dt)
                row = sig_features(sig)
                buf = self.sides[side]["frames"]
                buf.append(row)
                if len(buf) > self.window:
                    del buf[: len(buf) - self.window]
                if len(buf) == self.window and sig.present:
                    norm = (np.asarray(buf, dtype=np.float32) - self.sides[side]["mean"]) / self.sides[side]["std"]
                    probs = predict_proba_np(self.sides[side]["model"], norm[None, :, :])[0]
                    pidx = int(probs.argmax())
                    rec[side] = {
                        "present": True,
                        "raw_flex": list(sig.raw_flex),
                        "feature_row": row,
                        "pred": self.class_names[pidx],
                        "conf": float(probs[pidx]),
                        "probs": [float(p) for p in probs],
                    }
                else:
                    rec[side] = {
                        "present": sig.present,
                        "raw_flex": list(sig.raw_flex) if sig.present else None,
                        "feature_row": row,
                        "pred": "(warmup)",
                        "conf": 0.0,
                    }
            self.records.append(rec)
            if now - last_print >= 0.4:
                last_print = now
                pL = rec["L"]["pred"]
                pR = rec["R"]["pred"]
                print(f"    t={elapsed:4.1f}s  L→{pL:<24}  R→{pR}")
            await asyncio.sleep(max(0.0, dt - (time.monotonic() - now)))

    def _save(self) -> None:
        flex_now = {side: self.cond[side].get_flex_range() for side in ("L", "R")}
        meta = {
            "manifest": str(self.manifest_path),
            "class_names": self.class_names,
            "window": self.window,
            "secs_per_gesture": self.secs,
            "classes": self.classes,
            "live_flex_range": flex_now,
        }
        with self.out_path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"__meta__": meta}) + "\n")
            for rec in self.records:
                fh.write(json.dumps(rec) + "\n")
        print(f"Saved -> {self.out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--secs", type=float, default=4.0,
                    help="Seconds to record per gesture (default 4).")
    ap.add_argument("--calibrate-secs", type=float, default=15.0,
                    help="Initial wiggle-everything calibration window. 0 to skip.")
    ap.add_argument("--classes", type=str, default=DEFAULT_CLASSES,
                    help="Comma-separated list of gestures to prompt for.")
    ap.add_argument("--manifest", type=Path, default=None)
    ap.add_argument("--out-dir", type=Path,
                    default=Path("artifacts/reports/live_dumps"))
    args = ap.parse_args()
    mpath = args.manifest or active_manifest()
    classes = [c.strip() for c in args.classes.split(",") if c.strip()]
    rec = GestureRecorder(mpath, args.secs, classes, args.out_dir, args.calibrate_secs)
    try:
        asyncio.run(rec.run())
    except KeyboardInterrupt:
        print("\n[interrupted] saving partial recording…")
        rec._save()


if __name__ == "__main__":
    main()
