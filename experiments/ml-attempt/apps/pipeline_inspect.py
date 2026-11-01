#!/usr/bin/env python3
"""Terminal HUD: flex, rolling envelope, bend %, ML labels, region, last command."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collier.calibration import CombinedCalibration
from collier.conditioning import GloveConditioner as CollierConditioner
from collier.config import Config
from collier.frames import RawFrame
from collier.selection import SelectionResolver
from collier.transport import GloveTransport
from glove_ml_core.features import sig_features
from glove_ml_core.flex_live import SessionFlexTracker
from glove_ml_core.signals import GloveConditioner as V3Conditioner
from glove_ml_core.signals import GloveSignals as V3Signals
from ml.manifest_runtime import DualManifestInferencer

from collier_live.calib_io import try_load_forward_calibration
from collier_live.config import LiveConfig
from collier_live.postprocess import GesturePostProcessor


def _clear() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def _fmt_probs(names: list[str], probs, top: int = 3) -> str:
    if probs is None:
        return "-"
    idx = sorted(range(len(probs)), key=lambda i: -probs[i])[:top]
    parts = [f"{names[i]}:{probs[i]:.2f}" for i in idx]
    return " ".join(parts)


class PipelineInspect:
    def __init__(self, manifest: Path, live: LiveConfig):
        self.cfg = Config()
        self.cfg.selection.n_regions = live.n_regions
        self.live = live
        self._ml = DualManifestInferencer(manifest, refractory_s=live.infer_refractory_s)
        thresholds_by_side = {
            side: {
                str(k): float(v)
                for k, v in self._ml.manifest["sides"][side].get("class_thresholds", {}).items()
            }
            for side in ("L", "R")
        }
        self._post = GesturePostProcessor(
            live.post, live.n_regions, thresholds_by_side=thresholds_by_side
        )
        n_ch = self.cfg.packet.flex_count
        c_cfg, s_cfg = self.cfg.conditioning, self.cfg.selection
        self._cond_collier = {
            s: CollierConditioner(s, n_ch, c_cfg, s_cfg) for s in ("L", "R")
        }
        self._cond_ml = {"L": V3Conditioner("L"), "R": V3Conditioner("R")}
        self._flex_ml = {
            "L": SessionFlexTracker(n_ch, min_span=live.flex_min_span),
            "R": SessionFlexTracker(n_ch, min_span=live.flex_min_span),
        }
        fr = self._ml.flex_range
        if fr:
            for side in ("L", "R"):
                sd = fr.get(side)
                if sd:
                    self._cond_ml[side].set_flex_range(sd["lo"], sd["hi"])
        self._resolver = SelectionResolver(self.cfg.selection)
        self._calib = CombinedCalibration(self.cfg.selection)
        try_load_forward_calibration(self._resolver)
        self._latest: dict[str, RawFrame | None] = {"L": None, "R": None}
        self._transport = GloveTransport(self.cfg.ble, self.cfg.packet, self._on_frame)
        self._last_cmd = "-"
        self._hud_period = 1.0 / 8.0
        self._last_hud = 0.0

    def _on_frame(self, frame: RawFrame) -> None:
        self._latest[frame.side] = frame

    def _fresh(self, side: str, t: float) -> RawFrame | None:
        f = self._latest[side]
        if f is None:
            return None
        if t - f.recv_t > self.cfg.process.stale_after_s:
            return None
        return f

    async def run(self) -> None:
        print("Pipeline inspect (8 Hz HUD). Ctrl+C to quit.\n")
        await asyncio.gather(self._transport.run(), self._loop())

    async def _loop(self) -> None:
        dt = self.cfg.process.dt
        while True:
            t0 = time.monotonic()
            t = t0
            sigs: dict[str, V3Signals] = {}
            flex_info: dict[str, str] = {}
            collier_sigs = {}
            for side in ("L", "R"):
                raw = self._fresh(side, t)
                collier_sigs[side] = self._cond_collier[side].process(raw, dt)
                if raw is not None and not self._cond_ml[side].has_flex_range:
                    flex = self._flex_ml[side].update(tuple(raw.flex), dt)
                    if flex:
                        self._cond_ml[side].set_flex_range(flex["lo"], flex["hi"])
                sigs[side] = self._cond_ml[side].process(raw, dt)
                tr = self._flex_ml[side]
                flex_info[side] = (
                    f"cal=ok ML={'manifest' if self._cond_ml[side].has_flex_range else tr.channel_spans()[:3]}"
                )

            cl, cr = collier_sigs["L"], collier_sigs["R"]
            cal = self._calib.update(cl, cr, dt)
            if cal:
                lh, rh = cal
                self._resolver.calibrate("L", lh)
                self._resolver.calibrate("R", rh)
            sel = self._resolver.resolve(cl, cr)
            self._post.note_region("L", sel.left.region)
            self._post.note_region("R", sel.right.region)

            dec_l = self._ml.update("L", sigs["L"])
            dec_r = self._ml.update("R", sigs["R"])
            cmd_c = self._post.try_collective(
                dec_l.label, dec_r.label, dec_l.confidence, dec_r.confidence, sel
            )
            cmds = []
            if cmd_c:
                cmds.append(cmd_c)
            for side, dec in (("L", dec_l), ("R", dec_r)):
                c = self._post.process(side, dec.label, dec.confidence, sel)
                if c:
                    cmds.append(c)
            if cmds:
                self._last_cmd = " | ".join(f"{c.kind}@{c.regions} s={c.steps}" for c in cmds)

            if t0 - self._last_hud >= self._hud_period:
                self._last_hud = t0
                self._render(sigs, flex_info, sel, dec_l, dec_r)

            await asyncio.sleep(max(0.0, dt - (time.monotonic() - t0)))

    def _render(self, sigs, flex_info, sel, dec_l, dec_r) -> None:
        _clear()
        lines = [
            f"manifest: {self._ml.manifest_path.name}  arch={self._ml.model_arch}",
            f"calib: L={self._resolver.is_calibrated('L')} R={self._resolver.is_calibrated('R')}  "
            f"region L={sel.left.region} R={sel.right.region}",
            f"last_cmd: {self._last_cmd}",
            "",
        ]
        for side, dec in (("L", dec_l), ("R", dec_r)):
            sig = sigs[side]
            bend = [f"{b:.2f}" for b in sig.finger_bend[:5]] if sig.present else ["-"] * 5
            raw_avg = 0.0
            if sig.present and sig.raw_flex:
                raw_avg = sum(sig.raw_flex) / len(sig.raw_flex)
            lines.append(
                f"[{side}] flex={flex_info.get(side,'?')} raw_avg={raw_avg:.0f}  "
                f"bend={','.join(bend)}"
            )
            lines.append(
                f"      ML {dec.label} conf={dec.confidence:.2f}  "
                f"{_fmt_probs(self._ml.class_names, dec.probs)}"
            )
            row = sig_features(sig) if sig.present else None
            if row:
                lines.append(f"      feat[5:9]={row[5]:.2f},{row[6]:.2f},{row[7]:.2f},{row[8]:.2f} "
                             f"tilt_rate={row[9]:.2f}")
        lines.append("")
        lines.append("/expr/filters placeholder — use bend columns above for gate tuning")
        print("\n".join(lines))


def main() -> None:
    p = argparse.ArgumentParser(description="Pipeline inspect HUD")
    p.add_argument("--manifest", type=Path, default=None)
    p.add_argument("--regions", type=int, default=4)
    args = p.parse_args()
    live = LiveConfig(manifest_path=args.manifest, n_regions=args.regions)
    path = live.resolve_manifest()
    asyncio.run(PipelineInspect(path, live).run())


if __name__ == "__main__":
    main()
