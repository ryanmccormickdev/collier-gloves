#!/usr/bin/env python3
"""Standalone manual OSC tester for collier_live → Max (PyQt6).

Run from the repo root:
  python tools/manual_osc_gui.py

Sends the same OSC as live performance:
  /region/<i>/notes, /region/<i>/spawn, /expr/filters
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "host"  # qt_env + collier_live live under host/
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qt_env import bootstrap_qt  # noqa: E402

bootstrap_qt()

from PyQt6 import QtCore, QtWidgets  # noqa: E402

from collier_live.commands import GestureCommand  # noqa: E402
from collier_live.config import LiveConfig  # noqa: E402
from collier_live.preview.window import RegionCard  # noqa: E402
from collier_live.region_engine import RegionStateEngine  # noqa: E402
from collier_live.sinks import OscLiveSink  # noqa: E402

FINGER_LABELS = (
    "L thumb",
    "L index",
    "L middle",
    "L ring",
    "L pinky",
    "R thumb",
    "R index",
    "R middle",
    "R ring",
    "R pinky",
)

_BG = "#181b22"
_FG = "#dde"
_DIM = "#888"
_ACCENT = "#9cf"
_ML = "#b8d4ff"
_MONO = "font-family:monospace;"


class ManualOscController:
    """OSC + region state — no GUI dependencies."""

    def __init__(self) -> None:
        self.live = LiveConfig()
        self.host = self.live.osc.host
        self.port = self.live.osc.port
        self.n_regions = self.live.n_regions
        self.osc: OscLiveSink | None = None
        self._connected_key: tuple[str, int, int] | None = None
        self.engine = RegionStateEngine.from_live(
            self.n_regions,
            self.live.root_midi,
            self.live.scale_intervals,
            self.live.min_midi,
            self.live.max_midi,
        )

    def connect(self, host: str, port: int, n_regions: int) -> None:
        if n_regions != self.n_regions:
            self.n_regions = n_regions
            self.engine = RegionStateEngine.from_live(
                n_regions,
                self.live.root_midi,
                self.live.scale_intervals,
                self.live.min_midi,
                self.live.max_midi,
            )
        self.host = host
        self.port = port
        self.osc = OscLiveSink(host, port)
        self._connected_key = (host, port, n_regions)

    def osc_address(self, kind: str, region: int, value: object) -> str:
        if kind == "notes":
            return f"/region/{region}/notes"
        if kind == "spawn":
            return f"/region/{region}/spawn"
        return kind

    def _require_osc(self) -> OscLiveSink:
        if self.osc is None:
            raise RuntimeError("Not connected — set host/port and click Connect.")
        return self.osc

    def emit(self, cmd: GestureCommand) -> tuple[str, list[str]]:
        osc = self._require_osc()
        deltas = self.engine.apply(cmd)
        sent: list[str] = []
        for kind, region, value in self.engine.drain_changes(deltas):
            addr = self.osc_address(kind, region, value)
            osc.apply_delta(kind, region, value)
            sent.append(f"{addr} {value!r}")
        if not sent:
            if cmd.kind == "scale":
                hint = " (region not spawned yet — hit Spawn / mute first)"
            else:
                hint = ""
            return f"{cmd.kind} → no OSC change{hint}", sent
        return f"{cmd.kind} → " + ", ".join(sent), sent

    def send_filters(self, bends: list[float]) -> str:
        self._require_osc().send_filters(bends)
        preview = ", ".join(f"{b:.2f}" for b in bends)
        return f"/expr/filters [{preview}]"


class ManualOscWindow(QtWidgets.QMainWindow):
    def __init__(self, controller: ManualOscController):
        super().__init__()
        self.controller = controller
        self._stream_hz = controller.live.osc.filter_rate_hz

        self.setWindowTitle("Manual OSC")
        self.resize(980, 760)
        self.setStyleSheet(f"background:{_BG}; color:{_FG};")

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        header = QtWidgets.QLabel("Manual OSC")
        header.setStyleSheet(f"color:{_FG}; font-size:14pt; font-weight:bold;")
        root.addWidget(header)

        sub = QtWidgets.QLabel(
            "Drive Max with the same messages as collier_live. "
            "Open conductor.maxpat (udpreceive 7400) — incoming OSC prints to the Max console."
        )
        sub.setStyleSheet(f"color:{_DIM}; font-size:9pt;")
        sub.setWordWrap(True)
        root.addWidget(sub)

        # --- connection -----------------------------------------------------
        conn = self._group("OSC target")
        conn_row = QtWidgets.QHBoxLayout()
        conn.layout().addLayout(conn_row)

        self.host_edit = QtWidgets.QLineEdit(controller.host)
        self.host_edit.setPlaceholderText("127.0.0.1")
        self.port_spin = QtWidgets.QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(controller.port)
        self.regions_spin = QtWidgets.QSpinBox()
        self.regions_spin.setRange(1, 16)
        self.regions_spin.setValue(controller.n_regions)

        for label, widget in (
            ("Host", self.host_edit),
            ("Port", self.port_spin),
            ("Regions", self.regions_spin),
        ):
            conn_row.addWidget(QtWidgets.QLabel(label))
            conn_row.addWidget(widget)

        self.connect_btn = QtWidgets.QPushButton("Connect")
        self.connect_btn.clicked.connect(lambda: self._on_connect())
        conn_row.addWidget(self.connect_btn)

        ping_btn = QtWidgets.QPushButton("Ping Max")
        ping_btn.setToolTip("Spawn region 0 — should appear in Max’s `print osc` console")
        ping_btn.clicked.connect(self._ping_max)
        conn_row.addWidget(ping_btn)
        conn_row.addStretch(1)

        self.conn_lbl = QtWidgets.QLabel("Not connected")
        self.conn_lbl.setStyleSheet(f"color:{_DIM}; {_MONO} font-size:9pt;")
        conn_row.addWidget(self.conn_lbl)
        root.addWidget(conn)

        # --- region cards (up to 16; visibility follows region count) -------
        self.cards_row = QtWidgets.QHBoxLayout()
        self.cards = [RegionCard(i) for i in range(16)]
        for i, card in enumerate(self.cards):
            card.setVisible(i < controller.n_regions)
            self.cards_row.addWidget(card)
        root.addLayout(self.cards_row)

        # --- target + gestures ----------------------------------------------
        mid = QtWidgets.QHBoxLayout()

        tgt = self._group("Target region")
        tgt_lay = QtWidgets.QHBoxLayout()
        tgt.layout().addLayout(tgt_lay)
        self.all_regions = QtWidgets.QCheckBox("All regions")
        self.region_spin = QtWidgets.QSpinBox()
        self.region_spin.setRange(0, max(0, controller.n_regions - 1))
        tgt_lay.addWidget(self.all_regions)
        tgt_lay.addWidget(QtWidgets.QLabel("Region"))
        tgt_lay.addWidget(self.region_spin)
        tgt_lay.addStretch(1)
        mid.addWidget(tgt, stretch=1)

        gesture = self._group("Discrete gestures")
        g = QtWidgets.QGridLayout()
        gesture.layout().addLayout(g)
        btn_style = (
            "QPushButton { background:#2a3140; color:#dde; border:1px solid #444;"
            " border-radius:4px; padding:10px; font-size:11pt; font-weight:bold; }"
            "QPushButton:hover { background:#343d4f; }"
            "QPushButton:pressed { background:#1e2430; }"
        )
        buttons = (
            self._btn("Scale ▲", lambda: self._send_scale(+1)),
            self._btn("Scale ▼", lambda: self._send_scale(-1)),
            self._btn("Spawn / mute", self._send_spawn),
            self._btn("Collective ▲", lambda: self._send_collective(+1)),
            self._btn("Collective ▼", lambda: self._send_collective(-1)),
        )
        for b in buttons:
            b.setStyleSheet(btn_style)
        g.addWidget(buttons[0], 0, 0)
        g.addWidget(buttons[1], 1, 0)
        g.addWidget(buttons[2], 0, 1, 2, 1)
        g.addWidget(buttons[3], 0, 2)
        g.addWidget(buttons[4], 1, 2)
        for c in range(3):
            g.setColumnStretch(c, 1)
        mid.addWidget(gesture, stretch=2)
        root.addLayout(mid)

        # --- filters --------------------------------------------------------
        filters = self._group("Continuous filters  (/expr/filters)")
        fv = QtWidgets.QVBoxLayout()
        filters.layout().addLayout(fv)

        slider_row = QtWidgets.QHBoxLayout()
        left_col = QtWidgets.QVBoxLayout()
        right_col = QtWidgets.QVBoxLayout()
        slider_row.addLayout(left_col)
        slider_row.addLayout(right_col)
        fv.addLayout(slider_row)

        self.sliders: list[QtWidgets.QSlider] = []
        self.slider_vals: list[QtWidgets.QLabel] = []

        for i, label in enumerate(FINGER_LABELS):
            col = left_col if i < 5 else right_col
            if i == 0:
                col.addWidget(self._section("Left hand"))
            if i == 5:
                col.addWidget(self._section("Right hand"))

            row = QtWidgets.QHBoxLayout()
            row.addWidget(QtWidgets.QLabel(label))
            slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
            slider.setRange(0, 100)
            slider.setValue(0)
            slider.setStyleSheet(
                "QSlider::groove:horizontal { height:6px; background:#2a2f3a; border-radius:3px; }"
                "QSlider::handle:horizontal { width:14px; margin:-4px 0; background:#6a9; border-radius:7px; }"
                "QSlider::sub-page:horizontal { background:#3a5570; border-radius:3px; }"
            )
            val_lbl = QtWidgets.QLabel("0.00")
            val_lbl.setStyleSheet(f"color:{_ML}; {_MONO} font-size:9pt;")
            val_lbl.setFixedWidth(44)
            val_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
            slider.valueChanged.connect(
                lambda v, lbl=val_lbl: lbl.setText(f"{v / 100:.2f}")
            )
            row.addWidget(slider, stretch=1)
            row.addWidget(val_lbl)
            col.addLayout(row)
            self.sliders.append(slider)
            self.slider_vals.append(val_lbl)

        ctrl = QtWidgets.QHBoxLayout()
        self.stream_chk = QtWidgets.QCheckBox(f"Stream filters (~{self._stream_hz:.0f} Hz)")
        self.stream_chk.setStyleSheet(f"color:{_ACCENT}; font-weight:bold;")
        self.stream_chk.toggled.connect(self._on_stream_toggled)
        ctrl.addWidget(self.stream_chk)

        send_once = QtWidgets.QPushButton("Send once")
        send_once.clicked.connect(lambda: self._send_filters(quiet=False))
        ctrl.addWidget(send_once)

        open_btn = QtWidgets.QPushButton("All open (0)")
        open_btn.clicked.connect(lambda: self._set_all_sliders(0))
        ctrl.addWidget(open_btn)

        curl_btn = QtWidgets.QPushButton("All curled (1)")
        curl_btn.clicked.connect(lambda: self._set_all_sliders(100))
        ctrl.addWidget(curl_btn)

        ctrl.addStretch(1)
        fv.addLayout(ctrl)

        hint = QtWidgets.QLabel("0 = straight, 1 = curled — same Collier flex order as live.")
        hint.setStyleSheet(f"color:{_DIM}; font-size:9pt;")
        fv.addWidget(hint)
        root.addWidget(filters, stretch=1)

        self.status_lbl = QtWidgets.QLabel("Connect, then send gestures or stream filters.")
        self.status_lbl.setStyleSheet(f"color:{_DIM}; {_MONO} font-size:9pt;")
        self.status_lbl.setWordWrap(True)
        root.addWidget(self.status_lbl)

        self._stream_timer = QtCore.QTimer(self)
        self._stream_timer.setInterval(max(1, int(1000 / self._stream_hz)))
        self._stream_timer.timeout.connect(lambda: self._send_filters(quiet=True))

    def _btn(self, text: str, handler) -> QtWidgets.QPushButton:
        b = QtWidgets.QPushButton(text)
        b.clicked.connect(handler)
        return b

    def _group(self, title: str) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox(title)
        box.setStyleSheet(
            "QGroupBox { border:1px solid #333; border-radius:6px; margin-top:8px; "
            "padding-top:8px; color:#aaa; font-weight:bold; }"
            "QGroupBox::title { subcontrol-origin: margin; left:10px; padding:0 4px; }"
        )
        lay = QtWidgets.QVBoxLayout(box)
        lay.setContentsMargins(10, 14, 10, 10)
        return box

    def _section(self, text: str) -> QtWidgets.QLabel:
        lbl = QtWidgets.QLabel(text)
        lbl.setStyleSheet(f"color:{_ACCENT}; font-size:10pt; font-weight:bold;")
        return lbl

    def _set_status(self, msg: str) -> None:
        self.status_lbl.setText(msg)

    def _target_key(self) -> tuple[str, int, int]:
        host = self.host_edit.text().strip() or self.controller.host
        return (host, int(self.port_spin.value()), int(self.regions_spin.value()))

    def _on_connect(self, *, quiet: bool = False) -> bool:
        try:
            host, port, n_regions = self._target_key()
            if n_regions <= 0:
                raise ValueError("Regions must be >= 1")
            self.controller.connect(host, port, n_regions)
            self.region_spin.setMaximum(max(0, n_regions - 1))
            for i, card in enumerate(self.cards):
                card.setVisible(i < n_regions)
            self.conn_lbl.setText(f"OSC → {host}:{port}  ({n_regions} regions)")
            self.conn_lbl.setStyleSheet(f"color:{_ACCENT}; {_MONO} font-size:9pt;")
            self._set_status(f"Connected — UDP target {host}:{port}")
            self._refresh_cards()
            return True
        except Exception as e:
            self.controller.osc = None
            self.controller._connected_key = None
            self.conn_lbl.setText("Not connected")
            self.conn_lbl.setStyleSheet(f"color:{_DIM}; {_MONO} font-size:9pt;")
            if not quiet:
                QtWidgets.QMessageBox.critical(self, "Connect failed", str(e))
            else:
                self._set_status(f"Connect failed: {e}")
            return False

    def _sync_connection(self, *, quiet: bool = False) -> bool:
        key = self._target_key()
        if self.controller.osc is not None and self.controller._connected_key == key:
            return True
        return self._on_connect(quiet=quiet)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.controller.osc is None:
            self._on_connect(quiet=True)

    def _refresh_cards(self) -> None:
        n = self.controller.n_regions
        for i, card in enumerate(self.cards):
            if i >= n:
                card.refresh(None, False)
                continue
            st = self.controller.engine.regions[i]
            card.refresh(st.midi if st.spawn else None, st.spawn)

    def _ping_max(self) -> None:
        if not self._sync_connection():
            return
        try:
            cmd = GestureCommand("spawn", (0,), "manual")
            msg, packets = self.controller.emit(cmd)
            self._refresh_cards()
            self._set_status(f"Ping: {msg}")
            if not packets:
                self._set_status("Ping sent no packets — try Connect first")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Ping failed", str(e))

    def _selected_regions(self) -> tuple[int, ...]:
        n = self.controller.n_regions
        if self.all_regions.isChecked():
            return tuple(range(n))
        r = max(0, min(n - 1, int(self.region_spin.value())))
        self.region_spin.setValue(r)
        return (r,)

    def _send_scale(self, steps: int) -> None:
        if not self._sync_connection():
            return
        try:
            cmd = GestureCommand("scale", self._selected_regions(), "manual", steps=steps)
            msg, _packets = self.controller.emit(cmd)
            self._set_status(msg)
            self._refresh_cards()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Send failed", str(e))

    def _send_spawn(self) -> None:
        if not self._sync_connection():
            return
        try:
            cmd = GestureCommand("spawn", self._selected_regions(), "manual")
            msg, _packets = self.controller.emit(cmd)
            self._set_status(msg)
            self._refresh_cards()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Send failed", str(e))

    def _send_collective(self, steps: int) -> None:
        if not self._sync_connection():
            return
        try:
            cmd = GestureCommand(
                "scale",
                tuple(range(self.controller.n_regions)),
                "both",
                steps=steps,
            )
            msg, _packets = self.controller.emit(cmd)
            self._set_status(msg)
            self._refresh_cards()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Send failed", str(e))

    def _read_bends(self) -> list[float]:
        return [s.value() / 100.0 for s in self.sliders]

    def _send_filters(self, *, quiet: bool) -> None:
        if not self._sync_connection(quiet=quiet):
            self.stream_chk.blockSignals(True)
            self.stream_chk.setChecked(False)
            self.stream_chk.blockSignals(False)
            return
        try:
            msg = self.controller.send_filters(self._read_bends())
            if not quiet:
                self._set_status(f"Sent {msg}")
        except Exception as e:
            self.stream_chk.blockSignals(True)
            self.stream_chk.setChecked(False)
            self.stream_chk.blockSignals(False)
            self._set_status(f"Filter send failed: {e}")
            if not quiet:
                QtWidgets.QMessageBox.critical(self, "Send failed", str(e))

    def _set_all_sliders(self, value: int) -> None:
        for s in self.sliders:
            s.setValue(value)
        if self.stream_chk.isChecked():
            self._send_filters(quiet=True)

    def _on_stream_toggled(self, on: bool) -> None:
        if on:
            self._send_filters(quiet=False)
            self._stream_timer.start()
            self._set_status(f"Streaming /expr/filters at ~{self._stream_hz:.0f} Hz")
        else:
            self._stream_timer.stop()
            self._set_status("Filter stream stopped")

    def closeEvent(self, event) -> None:
        self._stream_timer.stop()
        super().closeEvent(event)


def main() -> None:
    controller = ManualOscController()
    app = QtWidgets.QApplication(sys.argv)
    window = ManualOscWindow(controller)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
