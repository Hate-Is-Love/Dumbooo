"""Reusable HUD building blocks: framed panels, headers, sparklines, meters."""

from __future__ import annotations

import collections

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from dashboard.styles import theme


class HudPanel(QFrame):
    """Dark panel with cyan corner brackets and an optional spaced title."""

    def __init__(self, title: str = "", parent: QWidget | None = None, tag: str = "") -> None:
        super().__init__(parent)
        self.setObjectName("Panel")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(14, 12, 14, 12)
        self._layout.setSpacing(8)
        if title:
            header = QHBoxLayout()
            label = QLabel(title.upper())
            label.setObjectName("HudLabel")
            header.addWidget(label)
            header.addStretch(1)
            if tag:
                t = QLabel(tag)
                t.setObjectName("TagMuted")
                header.addWidget(t)
            self._layout.addLayout(header)

    @property
    def body(self) -> QVBoxLayout:
        return self._layout

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        p = QPainter(self)
        p.setPen(QPen(theme.qcolor(theme.CYAN, 0.7), 1.5))
        w, h, s = self.width() - 1, self.height() - 1, 10
        for x, y, dx, dy in ((0, 0, 1, 1), (w, 0, -1, 1), (0, h, 1, -1), (w, h, -1, -1)):
            p.drawLine(QPointF(x, y), QPointF(x + dx * s, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + dy * s))
        p.end()


def hud_label(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setObjectName("HudLabel")
    return label


def hud_value(text: str = "—") -> QLabel:
    label = QLabel(text)
    label.setObjectName("HudValue")
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class Sparkline(QWidget):
    """Rolling line chart for a 0..100 metric. ``None`` samples leave gaps."""

    def __init__(self, capacity: int = 90, color: str = theme.CYAN, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._values: collections.deque[float | None] = collections.deque(maxlen=capacity)
        self._color = color
        self.setMinimumHeight(46)

    def add(self, value: float | None) -> None:
        self._values.append(value)
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(QPen(theme.qcolor(theme.LINE), 1))
        for frac in (0.25, 0.5, 0.75):
            p.drawLine(QPointF(0, h * frac), QPointF(w, h * frac))
        cap = self._values.maxlen or 1
        step = w / max(1, cap - 1)
        offset = cap - len(self._values)
        segment = QPolygonF()
        p.setPen(QPen(theme.qcolor(self._color, 0.9), 1.4))
        for i, v in enumerate(self._values):
            if v is None:
                if segment.size() > 1:
                    p.drawPolyline(segment)
                segment = QPolygonF()
                continue
            segment.append(QPointF((offset + i) * step, h - 2 - (h - 4) * max(0.0, min(100.0, v)) / 100))
        if segment.size() > 1:
            p.drawPolyline(segment)
        p.end()


class LevelBars(QWidget):
    """Scrolling bar meter of recent audio levels (0..1)."""

    def __init__(self, bars: int = 48, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._levels: collections.deque[float] = collections.deque([0.0] * bars, maxlen=bars)
        self._speech: collections.deque[bool] = collections.deque([False] * bars, maxlen=bars)
        self.setMinimumHeight(40)

    def add(self, level: float, speech: bool) -> None:
        self._levels.append(max(0.0, min(1.0, level)))
        self._speech.append(speech)
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        w, h = self.width(), self.height()
        n = len(self._levels)
        bw = w / n
        for i, (lv, sp) in enumerate(zip(self._levels, self._speech)):
            bh = max(1.0, lv * (h - 2))
            color = theme.ORANGE if sp else theme.CYAN_DIM
            p.fillRect(QRectF(i * bw + 1, h - bh, max(1.0, bw - 2), bh), theme.qcolor(color, 0.35 + 0.6 * lv))
        p.end()


class StatusDot(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(10, 10)
        self._color = theme.MUTED

    def set_color(self, color: str) -> None:
        self._color = color
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.qcolor(self._color))
        p.drawEllipse(1, 1, 8, 8)
        p.end()
