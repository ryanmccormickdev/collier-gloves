#!/usr/bin/env python3
"""Capture a live flex range (wiggle every finger) and inject it into the
active manifest's ``flex_range`` field. This is the single command that
fixes the cold-start envelope problem documented in
``docs/diagnosis_findings.md``.

Run:  python apps/diag_calibrate_inject.py [--secs 25] [--manifest PATH]

Workflow:
  1. Put on both gloves; let them connect (the script waits).
  2. For the SECS-long countdown shown on screen, wiggle every finger of both
     hands through their full range (full open <-> full closed, all five
     fingers, several times).
  3. The script writes the captured per-channel min/max into the manifest's
     ``flex_range`` field and saves a backup of the previous manifest.

After this, ``collier_live`` (and the pipeline_inspect HUD) will apply the
fixed range to the v3 conditioner on startup and feature normalization will
match what the model expects from the get-go.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
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
from glove_ml_core.flex_live import SessionFlexTracker

from apps._diag_shared import active_manifest, load_manifest


class CalibCapture:
    def __init__(self, secs: float, manifest_path: Path, also_apply_to_active: bool):
        self.cfg = Config()
        self.secs = secs
        n_ch = self.cfg.packet.flex_count
        self.tracker = {
            "L": SessionFlexTracker(n_ch, min_span=80.0, decay_per_s=0.0),
            "R": SessionFlexTracker(n_ch, min_span=80.0, decay_per_s=0.0),
        }
        self._latest: dict[str, RawFrame | None] = {"L": None, "R": None}
        self._transport = GloveTransport(self.cfg.ble, self.cfg.packet, self._on_frame)
        self.manifest_path = manifest_path
        self.also_apply_to_active = also_apply_to_active
        self.result: dict | None = None

    def _on_frame(self, frame: RawFrame) -> None:
        self._latest[frame.side] = frame

    def _fresh(self, side: str, t: float) -> RawFrame | None:
        f = self._latest[side]
        if f is None or t - f.recv_t > self.cfg.process.stale_after_s:
            return None
        return f

    async def run(self) -> None:
        await asyncio.gather(self._transport.run(), self._loop())

    async def _loop(self) -> None:
        dt = self.cfg.process.dt
        print("Waiting for both gloves to connect…")
        while self._latest["L"] is None or self._latest["R"] is None:
            await asyncio.sleep(0.1)
        for n in (3, 2, 1):
            print(f"Get ready to wiggle EVERY finger of BOTH hands … {n}")
            await asyncio.sleep(0.7)
        print(f"\nWIGGLE NOW for {self.secs:.0f}s\n")
        t0 = time.monotonic()
        last_print = 0.0
        while True:
            now = time.monotonic()
            for side in ("L", "R"):
                raw = self._fresh(side, now)
                if raw is not None:
                    self.tracker[side].update(tuple(raw.flex), dt)
            if now - last_print >= 0.5:
                last_print = now
                left = self.secs - (now - t0)
                spans = {s: self.tracker[s].channel_spans() for s in ("L", "R")}
                lo_L = min(spans["L"]) if spans["L"] else 0.0
                lo_R = min(spans["R"]) if spans["R"] else 0.0
                print(
                    f"  remaining {left:4.1f}s  "
                    f"L spans min/max={min(spans['L']):.0f}/{max(spans['L']):.0f}  "
                    f"R spans min/max={min(spans['R']):.0f}/{max(spans['R']):.0f}"
                )
            if now - t0 >= self.secs:
                break
            await asyncio.sleep(max(0.0, dt - (time.monotonic() - now)))
        flex = {}
        for side in ("L", "R"):
            tr = self.tracker[side]
            if not tr.ready:
                print(f"[!] no data for side {side}; aborting")
                return
            # Snapshot directly off the tracker's accumulated min/max without
            # polluting them with a fake "all zeros" update.
            flex[side] = {"lo": list(tr._lo), "hi": list(tr._hi)}
        self.result = flex
        self._patch_manifest(flex)

    def _patch_manifest(self, flex: dict) -> None:
        path = self.manifest_path
        manifest = load_manifest(path)
        backup = path.with_suffix(path.suffix + f".bak_{int(time.time())}")
        shutil.copy2(path, backup)
        manifest["flex_range"] = flex
        manifest["flex_range_source"] = {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "secs": self.secs,
            "tool": "apps/diag_calibrate_inject.py",
        }
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"\nPatched manifest -> {path}")
        print(f"Backup saved      -> {backup}")
        for side in ("L", "R"):
            lo = flex[side]["lo"]
            hi = flex[side]["hi"]
            spans = [h - l for l, h in zip(lo, hi)]
            print(f"  {side}: spans min={min(spans):.0f}  max={max(spans):.0f}  mean={np.mean(spans):.0f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--secs", type=float, default=25.0)
    ap.add_argument("--manifest", type=Path, default=None)
    args = ap.parse_args()
    mpath = args.manifest or active_manifest()
    cap = CalibCapture(args.secs, mpath, also_apply_to_active=True)
    try:
        asyncio.run(cap.run())
    except KeyboardInterrupt:
        print("[interrupted]")


if __name__ == "__main__":
    main()
