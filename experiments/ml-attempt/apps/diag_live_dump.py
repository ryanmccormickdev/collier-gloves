#!/usr/bin/env python3
"""Live capture for diagnosis: for N seconds, log every frame's raw flex,
v3 signals, full feature row, model probabilities, and the postprocess'
chosen label. Writes one JSONL line per tick to artifacts/reports/.

Run:  python apps/diag_live_dump.py --secs 20 --label "scale_up_R" \
          [--manifest PATH]

Use this **with gloves connected**. Recommended protocol per gesture:
  1. Power on both gloves and let them connect.
  2. Wiggle all 10 fingers fully open / closed a few times (calibrates the
     live SessionFlexTracker so the bend features have a meaningful range).
  3. Hold neutral for ~1s, then perform the gesture cleanly, hold the end
     pose ~1s, then return to neutral. Repeat 3–5 times in the window.

After collecting a few of these (one per gesture you care about), pass them
to apps/diag_live_vs_clips.py for the comparison.
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


class LiveDumper:
    def __init__(self, manifest_path: Path, secs: float, label: str, out_dir: Path,
                 use_manifest_flex: bool):
        self.cfg = Config()
        self.manifest_path = manifest_path
        self.manifest = load_manifest(manifest_path)
        self.class_names = list(self.manifest["class_names"])
        self.window = int(self.manifest["window_size"])
        self.secs = float(secs)
        self.label = label
        self.use_manifest_flex = use_manifest_flex

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
            "L": SessionFlexTracker(n_ch, min_span=80.0),
            "R": SessionFlexTracker(n_ch, min_span=80.0),
        }
        if use_manifest_flex:
            fr = self.manifest.get("flex_range") or {}
            for side in ("L", "R"):
                sd = fr.get(side) or fr.get(side.lower())
                if sd and "lo" in sd and "hi" in sd:
                    self.cond[side].set_flex_range(sd["lo"], sd["hi"])

        self._latest: dict[str, RawFrame | None] = {"L": None, "R": None}
        self._transport = GloveTransport(self.cfg.ble, self.cfg.packet, self._on_frame)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe_label = "".join(c if c.isalnum() or c in "_-" else "_" for c in label)
        self.out_path = out_dir / f"live_dump_{safe_label}_{stamp}.jsonl"
        self.records: list[dict] = []

    def _on_frame(self, frame: RawFrame) -> None:
        self._latest[frame.side] = frame

    def _fresh(self, side: str, t: float) -> RawFrame | None:
        f = self._latest[side]
        if f is None or t - f.recv_t > self.cfg.process.stale_after_s:
            return None
        return f

    async def run(self) -> None:
        print(
            "diag_live_dump: opening BLE… wave both gloves to connect."
            f"\nRecording for {self.secs:.0f}s once both gloves are present."
            f"\nManifest: {self.manifest_path.name}"
            f"\nFlex range from manifest: {'YES' if self.use_manifest_flex and (self.manifest.get('flex_range') or {}) else 'NO (using live SessionFlexTracker)'}"
            f"\nOut -> {self.out_path}\n"
        )
        await asyncio.gather(self._transport.run(), self._loop())

    async def _loop(self) -> None:
        dt = self.cfg.process.dt
        t_started = None
        last_print = 0.0
        while True:
            now = time.monotonic()
            ready = self._latest["L"] is not None and self._latest["R"] is not None
            if t_started is None:
                if ready:
                    t_started = now
                    print("Both gloves present. Recording now — perform the gesture.\n")
                await asyncio.sleep(0.05)
                continue
            elapsed = now - t_started
            if elapsed >= self.secs:
                self._save()
                return

            rec: dict = {"t": elapsed, "label": self.label}
            for side in ("L", "R"):
                raw = self._fresh(side, now)
                if raw is not None and not self.cond[side].has_flex_range:
                    flex = self.flex_tracker[side].update(tuple(raw.flex), dt)
                    if flex:
                        self.cond[side].set_flex_range(flex["lo"], flex["hi"])
                sig = self.cond[side].process(raw, dt)
                row = sig_features(sig)
                rec[side] = {
                    "present": sig.present,
                    "raw_flex": list(sig.raw_flex) if sig.present else None,
                    "wrist_tilt": sig.wrist_tilt,
                    "palm_roll": sig.palm_roll,
                    "forward_up": sig.forward_up,
                    "openness": sig.openness,
                    "vert_vel": sig.vert_vel,
                    "vel_along": sig.vel_along,
                    "vel_across": sig.vel_across,
                    "wrist_tilt_rate": sig.wrist_tilt_rate,
                    "palm_roll_rate": sig.palm_roll_rate,
                    "gyro_mag": sig.gyro_mag,
                    "finger_bend": list(sig.finger_bend) if sig.present else None,
                    "finger_osc": list(sig.finger_osc) if sig.present else None,
                    "feature_row": row,
                }
                # Roll into per-side window buffer and predict.
                buf = self.sides[side]["frames"]
                buf.append(row)
                if len(buf) > self.window:
                    del buf[: len(buf) - self.window]
                if len(buf) == self.window and sig.present:
                    arr = (np.asarray(buf, dtype=np.float32) - self.sides[side]["mean"]) / self.sides[side]["std"]
                    probs = predict_proba_np(self.sides[side]["model"], arr[None, :, :])[0]
                    top = np.argsort(-probs)[:4]
                    rec[side]["probs_top4"] = [
                        {"label": self.class_names[int(i)], "p": float(probs[int(i)])}
                        for i in top
                    ]
                    rec[side]["pred"] = self.class_names[int(probs.argmax())]
                else:
                    rec[side]["pred"] = "(warmup)"

            self.records.append(rec)

            if now - last_print >= 0.5:
                last_print = now
                pL = rec["L"].get("pred", "?")
                pR = rec["R"].get("pred", "?")
                bend_L = rec["L"]["finger_bend"]
                bend_R = rec["R"]["finger_bend"]
                bend_L_str = ",".join(f"{b:.2f}" for b in bend_L) if bend_L else "--"
                bend_R_str = ",".join(f"{b:.2f}" for b in bend_R) if bend_R else "--"
                print(
                    f"  t={elapsed:5.1f}s  L pred={pL:<22} bend={bend_L_str}  "
                    f"|  R pred={pR:<22} bend={bend_R_str}"
                )
            await asyncio.sleep(max(0.0, dt - (time.monotonic() - now)))

    def _save(self) -> None:
        meta = {
            "manifest": str(self.manifest_path),
            "label": self.label,
            "secs": self.secs,
            "use_manifest_flex": self.use_manifest_flex,
            "n_frames": len(self.records),
            "class_names": self.class_names,
            "window": self.window,
            "live_flex_range": {
                side: self.cond[side].get_flex_range() for side in ("L", "R")
            },
        }
        with self.out_path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"__meta__": meta}) + "\n")
            for rec in self.records:
                fh.write(json.dumps(rec) + "\n")
        print(f"\nSaved {len(self.records)} frames -> {self.out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--secs", type=float, default=20.0)
    ap.add_argument("--label", type=str, default="unknown",
                    help="Free-form name for the run (gesture you're going to perform).")
    ap.add_argument("--manifest", type=Path, default=None)
    ap.add_argument("--out-dir", type=Path,
                    default=Path("artifacts/reports/live_dumps"))
    ap.add_argument("--use-manifest-flex", action="store_true",
                    help="Apply manifest's saved flex_range (if any) instead of rolling envelope.")
    args = ap.parse_args()
    mpath = args.manifest or active_manifest()
    dumper = LiveDumper(mpath, args.secs, args.label, args.out_dir, args.use_manifest_flex)
    try:
        asyncio.run(dumper.run())
    except KeyboardInterrupt:
        print("\n[interrupted] saving what we have...")
        dumper._save()


if __name__ == "__main__":
    main()
