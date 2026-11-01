"""LiveConductor: BLE -> calibrate (collier) + ML (v3) -> regions -> OSC."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from collier.calibration import CombinedCalibration
from collier.conditioning import GloveConditioner as CollierConditioner
from collier.conditioning import GloveSignals as CollierSignals
from collier.config import Config
from collier.frames import RawFrame
from collier.selection import SelectionResolver
from collier.transport import GloveTransport
from glove_ml_core.flex_live import RollingFlexTracker
from glove_ml_core.signals import GloveConditioner as V3Conditioner
from glove_ml_core.signals import GloveSignals as V3Signals
from ml.manifest_runtime import DualManifestInferencer

from .calib_io import try_load_forward_calibration
from .config import LiveConfig
from .filter_pose import FilterPoseGate
from .pick_engine import HandTick, PickGestureEngine
from .postprocess import GesturePostProcessor, label_to_command
from .region_engine import RegionStateEngine
from .sinks import OscLiveSink

if TYPE_CHECKING:
    from .preview.bridge import LiveBridge

log = logging.getLogger(__name__)


class LiveConductor:
    """
    Two parallel signal paths from each ``RawFrame`` (same as Collier + trainer intent):

    * **Collier** ``GloveConditioner`` → heading calibration, region selection, GUI HUD.
    * **v3** ``glove_ml_core.GloveConditioner`` → 27-dim features → PyTorch inference.

    Calibration and ML run on every tick; neither blocks the other.
    """

    def __init__(
        self,
        live: LiveConfig,
        collier_cfg: Config | None = None,
        bridge: LiveBridge | None = None,
    ):
        self.live = live
        self.cfg = collier_cfg or Config()
        self.cfg.selection.n_regions = live.n_regions
        self.cfg.conditioning.flex_window_s = live.flex_window_s

        # Pick engine is the default heuristic gesture path; the TCN
        # inferencer is still loaded so the HUD/region engine keep their
        # interfaces, but it's only consulted when ``live.engine == "ml"``.
        self._engine_mode = live.engine
        self._manifest_path = live.resolve_manifest()
        self._ml = DualManifestInferencer(
            self._manifest_path, refractory_s=live.infer_refractory_s
        )
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
        self._pick = PickGestureEngine(live.pick_engine)
        self._filter_pose = FilterPoseGate(live.filter_pose)
        self._filter_streaming_prev = False
        self._tick_t = 0.0  # virtual clock used by the pick engine
        self._regions = RegionStateEngine.from_live(
            live.n_regions,
            live.root_midi,
            live.scale_intervals,
            live.min_midi,
            live.max_midi,
        )
        self._osc = OscLiveSink(live.osc.host, live.osc.port)

        n_ch = self.cfg.packet.flex_count
        c_cfg = self.cfg.conditioning
        s_cfg = self.cfg.selection
        # Collier-native conditioner: calibration + selection parity with collier/app.py
        self._cond_collier = {
            side: CollierConditioner(side, n_ch, c_cfg, s_cfg)
            for side in ("L", "R")
        }
        # v3 conditioner: ML feature parity with trainer
        self._cond_ml = {
            "L": V3Conditioner("L"),
            "R": V3Conditioner("R"),
        }
        self._flex_ml = {
            "L": RollingFlexTracker(
                n_ch, min_span=live.flex_min_span, window_s=live.flex_window_s
            ),
            "R": RollingFlexTracker(
                n_ch, min_span=live.flex_min_span, window_s=live.flex_window_s
            ),
        }
        self._apply_manifest_flex_ml()
        self._flex_from_manifest = {
            side: self._cond_ml[side].has_flex_range for side in ("L", "R")
        }

        self._resolver = SelectionResolver(self.cfg.selection)
        self._calib = CombinedCalibration(self.cfg.selection)
        self._loaded_calib = try_load_forward_calibration(self._resolver)

        self._latest: dict[str, RawFrame | None] = {"L": None, "R": None}
        self._transport = GloveTransport(
            self.cfg.ble, self.cfg.packet, self._on_frame
        )
        self._filter_period = 1.0 / live.osc.filter_rate_hz
        self._last_filter = 0.0
        self._bridge = bridge
        self._last_hud = 0.0
        self._gestures_armed_at: float | None = None
        self._gestures_live = False

    def _on_live_calibration(self, lh: float, rh: float) -> None:
        if self._gestures_armed_at is not None:
            return
        delay = max(0.0, self.live.gesture_arm_delay_s)
        self._gestures_armed_at = time.monotonic() + delay
        msg = f"Calibrated L={lh:.1f}° R={rh:.1f}°"
        if delay > 0:
            msg += f" — gestures in {delay:.0f}s"
        print(msg)
        if self._bridge is not None:
            self._bridge.log_event(msg)

    def _gestures_enabled(self, now: float) -> bool:
        if self._gestures_armed_at is None:
            return self._resolver.calibrated
        if now < self._gestures_armed_at:
            return False
        if not self._gestures_live:
            self._gestures_live = True
            self._pick.reset()
            if self.live.debug:
                log.info("gestures: armed")
            if self._bridge is not None:
                self._bridge.log_event("Gestures armed")
        return True

    def _apply_manifest_flex_ml(self) -> None:
        fr = self._ml.flex_range
        if not fr:
            return
        for side in ("L", "R"):
            sd = fr.get(side) or fr.get(side.lower())
            if sd and "lo" in sd and "hi" in sd:
                self._cond_ml[side].set_flex_range(sd["lo"], sd["hi"])

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
        engine_banner = (
            "PICK heuristic (no ML in hot path — pick→move→release)"
            if self._engine_mode == "pick"
            else f"ML manifest: {self._manifest_path.name}"
        )
        print(
            "Collier Live. Calibration (BOTH hands): point forward & level, "
            "hold still ~1 s, then pick/open each hand 3×.\n"
            f"Engine: {engine_banner}\n"
            f"OSC -> {self.live.osc.host}:{self.live.osc.port}\n"
        )
        if self._loaded_calib:
            print(f"Loaded saved forward headings from {self._loaded_calib}\n")
        await asyncio.gather(self._transport.run(), self._tick_loop())

    async def _tick_loop(self) -> None:
        dt = self.cfg.process.dt
        while True:
            t0 = time.monotonic()
            t = t0
            collier_sigs: dict[str, CollierSignals] = {}
            ml_sigs: dict[str, V3Signals] = {}

            for side in ("L", "R"):
                raw = self._fresh(side, t)

                # Collier path: sliding min/max window inside conditioner
                collier_sigs[side] = self._cond_collier[side].process(raw, dt)

                # ML path: rolling min/max window (or manifest flex_range) then v3 features
                if raw is not None and not self._flex_from_manifest.get(side, False):
                    flex = self._flex_ml[side].update(tuple(raw.flex), dt)
                    if flex:
                        self._cond_ml[side].set_flex_range(flex["lo"], flex["hi"])
                ml_sigs[side] = self._cond_ml[side].process(raw, dt)

            collier_l, collier_r = collier_sigs["L"], collier_sigs["R"]

            cal = self._calib.update(collier_l, collier_r, dt)
            if cal is not None:
                lh, rh = cal
                self._resolver.calibrate("L", lh)
                self._resolver.calibrate("R", rh)
                self._on_live_calibration(lh, rh)

            sel = self._resolver.resolve(collier_l, collier_r)
            self._post.note_region("L", sel.left.region)
            self._post.note_region("R", sel.right.region)

            # ML decisions are still computed so the HUD/preview can show
            # them, but only consulted as actual commands when in "ml" mode.
            dec_l = self._ml.update("L", ml_sigs["L"])
            dec_r = self._ml.update("R", ml_sigs["R"])

            cmds: list = []
            if self._gestures_enabled(t0):
                if self._engine_mode == "pick":
                    cmds.extend(self._pick_engine_step(dt, collier_sigs, ml_sigs, sel))
                else:
                    cmd_c = self._post.try_collective(
                        dec_l.label, dec_r.label, dec_l.confidence, dec_r.confidence, sel
                    )
                    if cmd_c:
                        cmds.append(cmd_c)
                    for side, dec in (("L", dec_l), ("R", dec_r)):
                        cmd = self._post.process(side, dec.label, dec.confidence, sel)
                        if cmd:
                            cmds.append(cmd)
                        elif (
                            self.live.debug
                            and dec.label not in ("none",)
                            and not dec.label.startswith("collective_")
                            and self._post.last_block_reason
                        ):
                            log.debug("%s", self._post.last_block_reason)
                        elif (
                            dec.label == "spawn_note"
                            and dec.confidence >= 0.6
                            and self._post.last_block_reason
                        ):
                            if self._bridge is not None:
                                self._bridge.log_event(
                                    f"[blocked] {self._post.last_block_reason}"
                                )

            for cmd in cmds:
                deltas = self._regions.apply(cmd)
                for kind, region, value in self._regions.drain_changes(deltas):
                    self._osc.apply_delta(kind, region, value)
                    if self.live.debug:
                        log.info("OSC %s %s %s", kind, region, value)
                if self._bridge is not None:
                    self._bridge.log_command(cmd)

            if self._bridge is not None and (t0 - self._last_hud) >= 0.125:
                self._last_hud = t0
                self._bridge.update(
                    collier_l=collier_l,
                    collier_r=collier_r,
                    sel=sel,
                    resolver=self._resolver,
                    calib=self._calib,
                    dec_l=dec_l,
                    dec_r=dec_r,
                    ml_sigs=ml_sigs,
                    flex_ml=self._flex_ml,
                    cond_ml=self._cond_ml,
                    regions=self._regions,
                    cfg=self.cfg,
                    manifest_name=self._manifest_path.name,
                    osc_target=f"{self.live.osc.host}:{self.live.osc.port}",
                )

            if t0 - self._last_filter >= self._filter_period:
                self._last_filter = t0
                streaming = self._filter_pose.update(collier_l, collier_r, dt)
                if streaming != self._filter_streaming_prev:
                    self._filter_streaming_prev = streaming
                    if streaming:
                        log.info("filters: streaming ON (/expr/filters)")
                    elif self.live.debug and self._filter_pose.cfg.enabled:
                        l_ok = self._filter_pose._hand_ok(collier_l, "L")
                        r_ok = self._filter_pose._hand_ok(collier_r, "R")
                        log.info(
                            "filters: streaming OFF  "
                            "(L in_pose=%s  R in_pose=%s  hold=%.2f/%.2fs)",
                            l_ok,
                            r_ok,
                            self._filter_pose._hold_s,
                            self._filter_pose.cfg.hold_s,
                        )
                if streaming:
                    filters = self._filter_vector(collier_l, collier_r)
                    self._osc.send_filters(filters)

            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.0, dt - elapsed))

    def _pick_engine_step(self, dt: float, collier_sigs, ml_sigs, sel) -> list:
        """Drive the heuristic engine using the *production* collier signals
        for the pick latch + spawn flutter (calibration-drift immune,
        already proven good in the original Collier) and the IMU-derived v3
        signals for vert_vel + palm_roll."""
        self._tick_t += dt

        def to_tick(side: str) -> HandTick:
            mlsig = ml_sigs[side]
            csig = collier_sigs[side]
            if (mlsig is None or not mlsig.present
                or csig is None or not csig.present):
                return HandTick.absent()
            return HandTick(
                present=True,
                picked=bool(sel.arm(side).picked),
                vert_vel=float(mlsig.vert_vel),
                palm_roll=float(mlsig.palm_roll),
            )

        events = self._pick.update(
            t=self._tick_t,
            dt=dt,
            L=to_tick("L"),
            R=to_tick("R"),
        )

        out: list = []
        for ev in events:
            if self._bridge is not None:
                self._bridge.log_event(f"pick: {ev.label} ({ev.side})")
            cmd = label_to_command(
                ev.label,
                ev.side if ev.side in ("L", "R") else "L",
                sel,
                self._post._region_hist,
                self.live.n_regions,
                self.live.post,
            )
            if cmd is not None:
                out.append(cmd)
        return out

    def _filter_vector(self, left: CollierSignals, right: CollierSignals) -> list[float]:
        """Ten normalized finger bends (Collier adaptive flex), L then R."""

        def bends(sig: CollierSignals) -> list[float]:
            if sig.present and sig.finger_bend:
                return list(sig.finger_bend[:5])
            return [0.0] * 5

        return bends(left) + bends(right)
