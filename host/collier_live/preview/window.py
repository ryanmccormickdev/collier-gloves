"""PyQt6 window for Collier ML Live."""
from __future__ import annotations

from PyQt6 import QtCore, QtWidgets

from collier.preview.theory import midi_name

from .bridge import LiveBridge
from .heading_arc import HeadingArcWidget

_BG = "#181b22"
_FG = "#dde"
_MUTED = "#c88"
_DIM = "#888"
_ACCENT = "#9cf"
_ML = "#b8d4ff"
_MONO = "font-family:monospace;"


class RegionCard(QtWidgets.QFrame):
    def __init__(self, index: int):
        super().__init__()
        self.index = index
        self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "QFrame { background:#1e222b; border:1px solid #333; border-radius:6px; }"
        )
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 8)

        self.title = QtWidgets.QLabel(f"Region {index}")
        self.title.setStyleSheet(f"color:{_DIM}; {_MONO} font-size:10pt;")
        v.addWidget(self.title)

        self.note_lbl = QtWidgets.QLabel("—")
        self.note_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.note_lbl.setStyleSheet(
            f"color:{_FG}; {_MONO} font-size:28pt; font-weight:bold;"
        )
        v.addWidget(self.note_lbl)

        self.spawn_lbl = QtWidgets.QLabel("spawn —")
        self.spawn_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.spawn_lbl.setStyleSheet(f"color:{_DIM}; {_MONO} font-size:9pt;")
        v.addWidget(self.spawn_lbl)

        self.meta_lbl = QtWidgets.QLabel("midi —")
        self.meta_lbl.setStyleSheet(f"color:{_DIM}; {_MONO} font-size:9pt;")
        v.addWidget(self.meta_lbl)

    def refresh(self, midi: int | None, spawn: bool) -> None:
        if midi is None:
            self.note_lbl.setText("—")
            self.note_lbl.setStyleSheet(
                f"color:{_DIM}; {_MONO} font-size:28pt; font-weight:bold;"
            )
            self.spawn_lbl.setText("idle")
            self.spawn_lbl.setStyleSheet(f"color:{_DIM}; {_MONO} font-size:9pt;")
            self.meta_lbl.setText("midi —")
            self.setStyleSheet(
                "QFrame { background:#1e222b; border:1px solid #333; border-radius:6px; }"
            )
            return

        self.note_lbl.setText(midi_name(midi))
        color = _FG if spawn else _DIM
        self.note_lbl.setStyleSheet(
            f"color:{color}; {_MONO} font-size:28pt; font-weight:bold;"
        )
        self.spawn_lbl.setText("SPAWN" if spawn else "muted")
        self.spawn_lbl.setStyleSheet(
            f"color:{_ACCENT if spawn else _MUTED}; {_MONO} font-size:9pt; font-weight:bold;"
        )
        self.meta_lbl.setText(f"midi {midi}")
        border = "#6a9" if spawn else "#333"
        self.setStyleSheet(
            f"QFrame {{ background:#1e222b; border:1px solid {border}; border-radius:6px; }}"
        )


class LiveWindow(QtWidgets.QMainWindow):
    def __init__(self, bridge: LiveBridge, n_regions: int = 4):
        super().__init__()
        self.bridge = bridge
        self.setWindowTitle("Collier ML Live")
        self.resize(1024, 860)
        self.setStyleSheet(f"background:{_BG}; color:{_FG};")

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        header = QtWidgets.QLabel("Collier ML Live")
        header.setStyleSheet(f"color:{_FG}; font-size:14pt; font-weight:bold;")
        root.addWidget(header)

        self.meta_lbl = QtWidgets.QLabel("")
        self.meta_lbl.setStyleSheet(f"color:{_DIM}; {_MONO} font-size:9pt;")
        root.addWidget(self.meta_lbl)

        arc_box = QtWidgets.QFrame()
        arc_box.setStyleSheet(
            "QFrame { background:#12151a; border:1px solid #333; border-radius:6px; }"
        )
        arc_lay = QtWidgets.QVBoxLayout(arc_box)
        arc_lay.setContentsMargins(8, 6, 8, 4)
        arc_title = QtWidgets.QLabel("Pointing (240° arc — forward up)")
        arc_title.setStyleSheet(f"color:{_DIM}; font-size:9pt;")
        arc_lay.addWidget(arc_title)
        self.heading_arc = HeadingArcWidget(n_regions, plane_half_deg=120.0)
        arc_lay.addWidget(self.heading_arc)
        root.addWidget(arc_box)

        cards_row = QtWidgets.QHBoxLayout()
        # Performer's perspective: region 3 on the left, 0 on the right.
        self.cards = [RegionCard(i) for i in reversed(range(n_regions))]
        for card in self.cards:
            cards_row.addWidget(card)
        root.addLayout(cards_row, stretch=1)

        strip = QtWidgets.QFrame()
        strip.setStyleSheet("QFrame { background:#12151a; border-radius:4px; }")
        sv = QtWidgets.QVBoxLayout(strip)
        sv.setContentsMargins(10, 8, 10, 8)

        cal_row = QtWidgets.QHBoxLayout()
        self.cal_lbl = QtWidgets.QLabel("[UNCAL]")
        self.cal_lbl.setStyleSheet(f"color:{_ACCENT}; {_MONO} font-size:10pt;")
        cal_row.addWidget(self.cal_lbl)
        self.cal_bar = QtWidgets.QProgressBar()
        self.cal_bar.setRange(0, 100)
        self.cal_bar.setTextVisible(False)
        self.cal_bar.setFixedHeight(8)
        self.cal_bar.setStyleSheet(
            "QProgressBar { background:#2a2f3a; border:none; border-radius:3px; }"
            "QProgressBar::chunk { background:#6a9; border-radius:3px; }"
        )
        cal_row.addWidget(self.cal_bar, stretch=1)
        sv.addLayout(cal_row)

        self.flags_lbl = QtWidgets.QLabel("")
        self.flags_lbl.setStyleSheet(f"color:{_DIM}; {_MONO} font-size:9pt;")
        sv.addWidget(self.flags_lbl)

        self.hud_lbl = QtWidgets.QLabel("L: —  |  R: —")
        self.hud_lbl.setStyleSheet(f"{_MONO} font-size:9pt;")
        self.hud_lbl.setWordWrap(True)
        sv.addWidget(self.hud_lbl)

        self.ml_lbl = QtWidgets.QLabel("")
        self.ml_lbl.setStyleSheet(f"color:{_ML}; {_MONO} font-size:9pt;")
        self.ml_lbl.setWordWrap(True)
        sv.addWidget(self.ml_lbl)

        self.bend_lbl = QtWidgets.QLabel("")
        self.bend_lbl.setStyleSheet(f"color:{_DIM}; {_MONO} font-size:9pt;")
        sv.addWidget(self.bend_lbl)

        self.flex_lbl = QtWidgets.QLabel("")
        self.flex_lbl.setStyleSheet(f"color:{_DIM}; {_MONO} font-size:9pt;")
        sv.addWidget(self.flex_lbl)

        self.event_lbl = QtWidgets.QLabel("—")
        self.event_lbl.setStyleSheet(f"color:{_DIM}; {_MONO} font-size:9pt;")
        sv.addWidget(self.event_lbl)

        hint = QtWidgets.QLabel(
            "Calibrate: both hands forward & level ~1 s, then pick/open each hand 3×."
        )
        hint.setStyleSheet(f"color:#555; font-size:9pt;")
        hint.setWordWrap(True)
        sv.addWidget(hint)

        root.addWidget(strip)

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)  # ~30 Hz UI
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

    def _refresh(self) -> None:
        s = self.bridge.snapshot()
        self.meta_lbl.setText(f"Model: {s.manifest_name}   OSC → {s.osc_target}")

        self.heading_arc.set_geometry(
            n_regions=s.n_regions, plane_half_deg=s.plane_half_deg
        )
        self.heading_arc.set_headings(
            left=s.left_heading,
            right=s.right_heading,
            left_present=s.left_present,
            right_present=s.right_present,
        )

        by_index = {rv.index: rv for rv in s.regions}
        for card in self.cards:
            rv = by_index.get(card.index)
            if rv is None:
                card.refresh(None, False)
            else:
                card.refresh(rv.midi, rv.spawn)

        self.cal_lbl.setText(f"[{s.cal_label}]")
        self.cal_bar.setValue(int(s.cal_progress * 100))
        self.flags_lbl.setText(" ".join(s.flags) if s.flags else "")
        self.hud_lbl.setText(
            f"L: {s.left_line}\nR: {s.right_line}\n"
            f"target region: L={s.region_l}  R={s.region_r}"
        )
        self.ml_lbl.setText(f"{s.left_ml}\n{s.right_ml}")
        self.bend_lbl.setText(f"bend L [{s.left_bend}]  R [{s.right_bend}]")
        self.flex_lbl.setText(f"{s.left_flex}   |   {s.right_flex}")
        self.event_lbl.setText(s.last_events[-1] if s.last_events else "—")
