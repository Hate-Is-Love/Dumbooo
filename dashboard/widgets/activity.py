"""HUD activity indicator for database work (retrieving, saving, exporting, deleting).

Styled after the data read-outs in the reference video: a spinning segmented
ring, a scanning bar with moving data blocks and an uppercase status label.
It animates only while real work is running (``begin`` … ``end``), then shows
a short green (success) or red (failure) flash and goes idle, so it costs
nothing when nothing is happening.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from dashboard.styles import theme

FLASH_MS = 1400


class ScanIndicator(QWidget):
    def __init__(self, idle_text: str = "DATA LINK READY", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(34)
        self.setMinimumWidth(240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._idle_text = idle_text
        self._label = idle_text
        self._active = 0            # nested begin() count
        self._result: bool | None = None
        self._clock = QElapsedTimer()
        self._clock.start()
        self._flash_until = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.operations = 0         # how many operations ran (used by tests)

    # -- API -------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self._active > 0

    def begin(self, label: str) -> None:
        self._active += 1
        self.operations += 1
        self._label = label.upper()
        self._result = None
        if not self._timer.isActive():
            self._timer.start(33)
        self.update()

    def end(self, ok: bool = True, label: str | None = None) -> None:
        self._active = max(0, self._active - 1)
        if self._active:
            return
        self._result = ok
        self._label = (label or ("COMPLETE" if ok else "FAILED")).upper()
        self._flash_until = self._clock.elapsed() + FLASH_MS
        self.update()

    def _tick(self) -> None:
        if not self._active and self._clock.elapsed() > self._flash_until:
            self._timer.stop()
            self._label = self._idle_text
            self._result = None
        self.update()

    # -- painting -----------------------------------------------------------------
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        t = self._clock.elapsed() / 1000
        if self._active:
            color = theme.CYAN
        elif self._result is True:
            color = theme.GREEN
        elif self._result is False:
            color = theme.RED
        else:
            color = theme.LINE_BRIGHT
        p.fillRect(self.rect(), theme.qcolor(theme.BG_PANEL_2, 0.9))
        p.setPen(QPen(theme.qcolor(color, 0.6), 1))
        p.drawRect(QRectF(0.5, 0.5, w - 1, h - 1))

        # spinning segmented ring
        cx, cy, r = 17, h / 2, 10
        ring = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        spin = (t * 300) % 360 if self._active else 0
        for i in range(6):
            p.setPen(QPen(theme.qcolor(color, 0.35 + 0.65 * (i % 2)), 2.2, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.FlatCap))
            p.drawArc(ring, int((spin + i * 60) * 16), int(36 * 16))
        p.setBrush(theme.qcolor(color, 0.9 if self._active or self._result is not None else 0.3))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), 2.5, 2.5)

        # label
        font = QFont("Consolas", 8)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2)
        p.setFont(font)
        p.setPen(theme.qcolor(color if self._active or self._result is not None else theme.MUTED))
        dots = "." * (int(t * 3) % 4) if self._active else ""
        p.drawText(QRectF(36, 0, w * 0.5, h), Qt.AlignmentFlag.AlignVCenter, self._label + dots)

        # data stream: blocks sliding through a track, plus a scanning highlight
        x0, x1 = max(36 + w * 0.42, 180), w - 10
        track = QRectF(x0, h / 2 - 5, max(10, x1 - x0), 10)
        p.fillRect(track, theme.qcolor(theme.LINE, 0.8))
        if self._active:
            n = max(6, int(track.width() // 9))
            for i in range(n):
                phase = (t * 1.6 + i / n) % 1.0
                bx = track.left() + phase * track.width()
                alpha = 0.25 + 0.75 * abs(math.sin((i + t * 4) * 1.3))
                p.fillRect(QRectF(bx, track.top() + 2, 5, 6), theme.qcolor(color, alpha))
            sweep = track.left() + ((t * 0.9) % 1.0) * track.width()
            p.fillRect(QRectF(sweep - 14, track.top(), 28, track.height()), theme.qcolor(theme.PALE, 0.35))
        elif self._result is not None:
            remaining = max(0.0, (self._flash_until - self._clock.elapsed()) / FLASH_MS)
            p.fillRect(QRectF(track.left(), track.top(), track.width() * remaining, track.height()),
                       theme.qcolor(color, 0.7))
        p.end()
