"""240° pointing arc — region wedges + L/R heading vectors."""
from __future__ import annotations

import math

from PyQt6 import QtCore, QtGui, QtWidgets

_REGION_COLORS = ("#4a7eb8", "#5a9e72", "#c4a24a", "#b06a9a", "#8a6eb8", "#6eb8b0")

_L_COLOR = QtGui.QColor("#7ec8ff")
_R_COLOR = QtGui.QColor("#ffb07a")


class HeadingArcWidget(QtWidgets.QWidget):
    """Arc spans [-plane_half, +plane_half] with forward (0°) at the top.

    Headings are mirrored horizontally so the wedge layout matches the region
    cards (region 3 on the left, 0 on the right — performer's perspective).
    """

    def __init__(self, n_regions: int = 4, plane_half_deg: float = 120.0):
        super().__init__()
        self._n_regions = max(1, n_regions)
        self._plane_half = plane_half_deg
        self._left_heading: float | None = None
        self._right_heading: float | None = None
        self._left_present = False
        self._right_present = False
        self.setMinimumHeight(480)
        self.setMaximumHeight(660)

    def set_geometry(self, *, n_regions: int, plane_half_deg: float) -> None:
        self._n_regions = max(1, n_regions)
        self._plane_half = plane_half_deg
        self.update()

    def set_headings(
        self,
        *,
        left: float | None,
        right: float | None,
        left_present: bool,
        right_present: bool,
    ) -> None:
        self._left_heading = left
        self._right_heading = right
        self._left_present = left_present
        self._right_present = right_present
        self.update()

    @staticmethod
    def _mirror(heading_deg: float) -> float:
        """Flip L/R so the arc matches the reversed region card row."""
        return -heading_deg

    @staticmethod
    def _qt_start_sixteenths(heading_deg: float) -> int:
        return int((90.0 - heading_deg) * 16.0)

    def _point(self, cx: float, cy: float, radius: float, heading_deg: float) -> QtCore.QPointF:
        h = self._mirror(heading_deg)
        rad = math.radians(h)
        return QtCore.QPointF(
            cx + radius * math.sin(rad),
            cy - radius * math.cos(rad),
        )

    def paintEvent(self, _event) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        cx = w * 0.5
        cy = h - 42.0
        r = min(w * 0.46, cy - 66.0)
        if r < 72.0:
            return

        rect = QtCore.QRectF(cx - r, cy - r, 2 * r, 2 * r)
        half = self._plane_half
        width = (2.0 * half) / self._n_regions

        p.setPen(QtCore.Qt.PenStyle.NoPen)
        for i in range(self._n_regions):
            start_h = -half + i * width
            end_h = start_h + width
            disp_start = self._mirror(start_h)
            disp_end = self._mirror(end_h)
            color = QtGui.QColor(_REGION_COLORS[i % len(_REGION_COLORS)])
            color.setAlpha(110)
            p.setBrush(color)
            span = int((disp_start - disp_end) * 16.0)
            start = self._qt_start_sixteenths(disp_start)
            p.drawPie(rect, start, span)

        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        p.setPen(QtGui.QPen(QtGui.QColor("#555"), 2.0))
        p.drawArc(
            rect,
            self._qt_start_sixteenths(self._mirror(+half)),
            int(-2 * half * 16.0),
        )

        p.setFont(QtGui.QFont("Helvetica", 12, QtGui.QFont.Weight.Bold))
        for i in range(self._n_regions):
            start_h = -half + i * width
            mid_h = start_h + width * 0.5
            label_pt = self._point(cx, cy, r * 0.62, mid_h)
            p.setPen(QtGui.QPen(QtGui.QColor(_REGION_COLORS[i % len(_REGION_COLORS)])))
            p.drawText(int(label_pt.x() - 8), int(label_pt.y() + 6), str(i))

        p.setPen(QtGui.QPen(QtGui.QColor("#888"), 1.5, QtCore.Qt.PenStyle.DashLine))
        p.drawLine(QtCore.QPointF(cx, cy), self._point(cx, cy, r, 0.0))

        for label, heading, present, color in (
            ("L", self._left_heading, self._left_present, _L_COLOR),
            ("R", self._right_heading, self._right_present, _R_COLOR),
        ):
            if not present or heading is None or abs(heading) > half + 6.0:
                continue
            tip = self._point(cx, cy, r * 0.88, heading)
            p.setPen(QtGui.QPen(color, 4.0))
            p.drawLine(QtCore.QPointF(cx, cy), tip)
            p.setBrush(color)
            p.drawEllipse(tip, 7.0, 7.0)
            p.setPen(QtGui.QPen(color))
            p.setFont(QtGui.QFont("Helvetica", 12, QtGui.QFont.Weight.Bold))
            p.drawText(int(tip.x() + 10), int(tip.y() + 6), label)

        p.setPen(QtGui.QPen(QtGui.QColor("#666")))
        p.setFont(QtGui.QFont("Helvetica", 10))
        p.drawText(int(cx - 14), int(cy + 22), "fwd")
        p.drawText(8, int(cy - 6), f"+{half:.0f}°")
        p.drawText(int(w - 52), int(cy - 6), f"−{half:.0f}°")
