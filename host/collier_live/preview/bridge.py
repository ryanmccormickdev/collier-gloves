"""Conductor → GUI bridge for Collier ML Live."""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field

from collier.calibration import CombinedCalibration
from collier.conditioning import GloveSignals
from collier.config import Config
from collier.preview.hud import format_arm, format_flags
from collier.selection import Selection, SelectionResolver
from glove_ml_core.flex_live import RollingFlexTracker, SessionFlexTracker
from glove_ml_core.signals import GloveConditioner, GloveSignals as V3Signals
from ml.manifest_runtime import HandDecision

from ..region_engine import RegionStateEngine
from .hud import format_bend, format_flex_status, format_ml_line


@dataclass
class RegionView:
    index: int
    midi: int | None
    spawn: bool


@dataclass
class LiveSnapshot:
    manifest_name: str = ""
    osc_target: str = ""
    cal_label: str = "UNCAL"
    cal_progress: float = 0.0
    flags: list[str] = field(default_factory=list)
    left_line: str = ""
    right_line: str = ""
    left_ml: str = ""
    right_ml: str = ""
    left_bend: str = ""
    right_bend: str = ""
    left_flex: str = ""
    right_flex: str = ""
    region_l: str = "-"
    region_r: str = "-"
    left_heading: float | None = None
    right_heading: float | None = None
    left_present: bool = False
    right_present: bool = False
    plane_half_deg: float = 120.0
    n_regions: int = 4
    regions: list[RegionView] = field(default_factory=list)
    last_events: list[str] = field(default_factory=list)


class LiveBridge:
    def __init__(self, class_names: list[str]):
        self.class_names = class_names
        self._lock = threading.Lock()
        self._snap = LiveSnapshot()
        self._events: deque[str] = deque(maxlen=10)

    def log_event(self, line: str) -> None:
        with self._lock:
            self._events.append(line)

    def log_command(self, cmd) -> None:
        self.log_event(
            f"[cmd] {cmd.kind} regions={cmd.regions} hand={cmd.source_hand} steps={cmd.steps}"
        )

    def update(
        self,
        *,
        collier_l: GloveSignals,
        collier_r: GloveSignals,
        sel: Selection,
        resolver: SelectionResolver,
        calib: CombinedCalibration,
        dec_l: HandDecision,
        dec_r: HandDecision,
        ml_sigs: dict[str, V3Signals],
        flex_ml: dict[str, RollingFlexTracker | SessionFlexTracker],
        cond_ml: dict[str, GloveConditioner],
        regions: RegionStateEngine,
        cfg: Config,
        manifest_name: str,
        osc_target: str,
    ) -> None:
        calibrated = resolver.calibrated
        cal_label = "cal" if calibrated else "UNCAL"
        cal_prog = calib.progress if calib.active else 0.0
        flags = format_flags(
            collier_l, collier_r, sel, calibrated, {"B": cal_prog}, cfg,
        )
        if not cond_ml["L"].has_flex_range and not flex_ml["L"].ready:
            flags.append("ML-FLEX-L")
        if not cond_ml["R"].has_flex_range and not flex_ml["R"].ready:
            flags.append("ML-FLEX-R")

        region_views = [
            RegionView(i, st.midi, st.spawn)
            for i, st in sorted(regions.regions.items())
        ]

        with self._lock:
            self._snap = LiveSnapshot(
                manifest_name=manifest_name,
                osc_target=osc_target,
                cal_label=cal_label,
                cal_progress=cal_prog,
                flags=flags,
                left_line=format_arm(collier_l, sel.left),
                right_line=format_arm(collier_r, sel.right),
                left_ml=format_ml_line("L", dec_l, self.class_names),
                right_ml=format_ml_line("R", dec_r, self.class_names),
                left_bend=format_bend(collier_l),
                right_bend=format_bend(collier_r),
                left_flex=self._flex_line("L", flex_ml, cond_ml),
                right_flex=self._flex_line("R", flex_ml, cond_ml),
                region_l=str(sel.left.region) if sel.left.region is not None else "-",
                region_r=str(sel.right.region) if sel.right.region is not None else "-",
                left_heading=sel.left.heading,
                right_heading=sel.right.heading,
                left_present=sel.left.present,
                right_present=sel.right.present,
                plane_half_deg=cfg.selection.plane_half_deg,
                n_regions=cfg.selection.n_regions,
                regions=region_views,
                last_events=list(self._events),
            )

    @staticmethod
    def _flex_line(
        side: str,
        flex_ml: dict[str, RollingFlexTracker | SessionFlexTracker],
        cond_ml: dict[str, GloveConditioner],
    ) -> str:
        tr = flex_ml[side]
        window_s = float(getattr(tr, "window_s", 60.0))
        cal = f"{side} cal: {window_s:.0f}s flex window"
        if cond_ml[side].has_flex_range:
            ml = f"{side} ML: manifest flex"
        else:
            ml = format_flex_status(
                side,
                ready=tr.ready,
                manifest_flex=False,
                spans=tr.channel_spans(),
                label="ML",
                window_s=window_s,
            )
        return f"{cal}  |  {ml}"

    def snapshot(self) -> LiveSnapshot:
        with self._lock:
            s = self._snap
            return LiveSnapshot(
                manifest_name=s.manifest_name,
                osc_target=s.osc_target,
                cal_label=s.cal_label,
                cal_progress=s.cal_progress,
                flags=list(s.flags),
                left_line=s.left_line,
                right_line=s.right_line,
                left_ml=s.left_ml,
                right_ml=s.right_ml,
                left_bend=s.left_bend,
                right_bend=s.right_bend,
                left_flex=s.left_flex,
                right_flex=s.right_flex,
                region_l=s.region_l,
                region_r=s.region_r,
                left_heading=s.left_heading,
                right_heading=s.right_heading,
                left_present=s.left_present,
                right_present=s.right_present,
                plane_half_deg=s.plane_half_deg,
                n_regions=s.n_regions,
                regions=list(s.regions),
                last_events=list(s.last_events),
            )
