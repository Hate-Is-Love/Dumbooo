"""Animated identity scanner shown on the sign-in / owner-setup screen.

Modes (driven by the real authentication result, never by a timer guess):

* ``idle``      — slow rotating rings, "AWAITING IDENTIFICATION"
* ``scanning``  — scan line sweeps over a fingerprint-like ridge pattern while the
                  password hash is being verified on a worker thread
* ``granted``   — green expanding rings, "ACCESS GRANTED"
* ``denied``    — red flicker and a short shake, "ACCESS DENIED"
* ``setup``     — amber, "OWNER INITIALISATION"
* ``face``      — the live camera picture inside the ring (mirrored), a frame around the
                  face, a progress arc for the face-check steps and a red CAMERA ON mark
"""

from __future__ import annotations

import math
import random

from PySide6.QtCore import QElapsedTimer, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QFont, QImage, QPainter, QPainterPath, QPen, QRadialGradient
from PySide6.QtWidgets import QSizePolicy, QWidget

from dashboard.styles import theme

TAU = math.tau
MODE_COLORS = {"idle": theme.CYAN_DIM, "scanning": theme.CYAN, "granted": theme.GREEN, "denied": theme.RED,
               "setup": theme.AMBER, "face": theme.CYAN}
MODE_TEXT = {"idle": "AWAITING IDENTIFICATION", "scanning": "VERIFYING IDENTITY", "granted": "ACCESS GRANTED",
             "denied": "ACCESS DENIED", "setup": "OWNER INITIALISATION", "face": "FACE SCAN"}


class IdentityScanner(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(320, 320)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.mode = "idle"
        self.subtitle = ""
        self._camera: QImage | None = None
        self._face_box: QRect | None = None
        self._face_step = (0, 1)
        self._clock = QElapsedTimer()
        self._clock.start()
        self._mode_ms = 0
        rng = random.Random(11)
        # fingerprint ridges: concentric broken arcs with slight wobble
        self._ridges = [(0.18 + i * 0.055, [(rng.uniform(0, 360), rng.uniform(40, 150)) for _ in range(rng.randint(2, 4))])
                        for i in range(11)]
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.setInterval(33)

    def set_mode(self, mode: str, subtitle: str = "") -> None:
        self.mode = mode if mode in MODE_COLORS else "idle"
        self.subtitle = subtitle
        self._mode_ms = self._clock.elapsed()
        self.update()

    def set_camera(self, image: QImage | None, box: QRect | None = None, step: int = 0, steps: int = 1) -> None:
        """Show a camera frame (face mode). ``None`` removes the picture (camera off)."""
        self._camera, self._face_box, self._face_step = image, box, (step, max(1, steps))
        self.update()

    def showEvent(self, event) -> None:
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def _paint_camera(self, p: QPainter, R: float, color: str, t: float) -> None:
        image, r = self._camera, R * 0.78
        scale = max(2 * r / image.width(), 2 * r / image.height())  # cover the circle
        w, h = image.width() * scale, image.height() * scale
        clip = QPainterPath()
        clip.addEllipse(QPointF(0, 0), r, r)
        p.save()
        p.setClipPath(clip)
        p.setOpacity(0.92)
        p.drawImage(QRectF(-w / 2, -h / 2, w, h), image)
        p.setOpacity(1.0)
        p.fillRect(QRectF(-r, -r, 2 * r, 2 * r), theme.qcolor(color, 0.10))  # a cyan tint, like the HUD
        if self._face_box is not None:  # corner brackets around the face
            b = self._face_box
            x, y, bw, bh = b.x() * scale - w / 2, b.y() * scale - h / 2, b.width() * scale, b.height() * scale
            c = min(bw, bh) * 0.22
            p.setPen(QPen(theme.qcolor(theme.GREEN if self._face_step[0] else color, 0.95), 2.4))
            for (px, py, dx, dy) in ((x, y, 1, 1), (x + bw, y, -1, 1), (x, y + bh, 1, -1), (x + bw, y + bh, -1, -1)):
                p.drawLine(QPointF(px, py), QPointF(px + dx * c, py))
                p.drawLine(QPointF(px, py), QPointF(px, py + dy * c))
        scan = math.sin(t * 2.4) * r * 0.9
        p.setPen(QPen(theme.qcolor(theme.PALE, 0.55), 1.6))
        p.drawLine(QPointF(-r, scan), QPointF(r, scan))
        p.restore()
        step, steps = self._face_step  # progress arc for the check's steps
        p.setPen(QPen(theme.qcolor(theme.GREEN, 0.9), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(QRectF(-r - 8, -r - 8, 2 * r + 16, 2 * r + 16), 90 * 16, int(-360 * 16 * step / steps))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.qcolor(theme.RED, 0.6 + 0.4 * math.sin(t * 4)))
        p.drawEllipse(QPointF(-r * 0.62, -r - 22), 4, 4)
        p.setPen(theme.qcolor(theme.RED))
        p.setFont(QFont("Consolas", 8))
        p.drawText(QPointF(-r * 0.62 + 9, -r - 18), "CAMERA ON")

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        t = self._clock.elapsed() / 1000
        since = (self._clock.elapsed() - self._mode_ms) / 1000
        color = MODE_COLORS[self.mode]
        shake = math.sin(since * 60) * 8 * max(0.0, 0.5 - since) if self.mode == "denied" else 0.0
        cx, cy = w / 2 + shake, h / 2 - 20
        R = min(w, h - 80) * 0.40
        flicker = 0.6 + 0.4 * math.sin(t * 40) if self.mode == "denied" and since < 1.2 else 1.0

        glow = QRadialGradient(QPointF(cx, cy), R * 1.5)
        glow.setColorAt(0, theme.qcolor(color, 0.18))
        glow.setColorAt(1, theme.qcolor(theme.BG, 0))
        p.fillRect(self.rect(), QBrush(glow))
        p.translate(cx, cy)

        # hexagon frame
        hexagon = QPainterPath()
        for i in range(7):
            a = TAU * i / 6 + TAU / 12
            point = QPointF(math.cos(a) * R * 1.12, math.sin(a) * R * 1.12)
            hexagon.moveTo(point) if i == 0 else hexagon.lineTo(point)
        p.setPen(QPen(theme.qcolor(color, 0.45 * flicker), 1.4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(hexagon)

        # rotating tick ring
        speed = {"idle": 12, "scanning": 90, "granted": 30, "denied": 5, "setup": 25, "face": 40}[self.mode]
        p.save()
        p.rotate(t * speed)
        for i in range(72):
            a = TAU * i / 72
            inner = R * (0.93 if i % 6 else 0.88)
            p.setPen(QPen(theme.qcolor(color, (0.35 if i % 6 else 0.8) * flicker), 1.2))
            p.drawLine(QPointF(math.cos(a) * inner, math.sin(a) * inner), QPointF(math.cos(a) * R, math.sin(a) * R))
        p.restore()
        p.save()
        p.rotate(-t * speed * 0.6)
        rect = QRectF(-R * 0.8, -R * 0.8, R * 1.6, R * 1.6)
        for i in range(4):
            p.setPen(QPen(theme.qcolor(color, 0.8 * flicker), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.drawArc(rect, int(i * 90 * 16), int(50 * 16))
        p.restore()

        if self._camera is not None:
            self._paint_camera(p, R, color, t)
        # fingerprint ridges, lit up below the scan line
        scan_y = None
        if self.mode == "scanning":
            scan_y = math.sin(t * 3.2) * R * 0.62
        for radius_frac, arcs in ([] if self._camera is not None else self._ridges):
            r = R * radius_frac * 1.15
            rr = QRectF(-r * 0.82, -r, r * 1.64, r * 2)
            for start, span in arcs:
                lit = 0.25
                if self.mode == "granted":
                    lit = 0.9
                elif scan_y is not None:
                    lit = 0.25 + 0.75 * max(0.0, 1 - abs(scan_y + r * math.sin(math.radians(start))) / (R * 0.35))
                p.setPen(QPen(theme.qcolor(color, lit * flicker), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawArc(rr, int((start + math.sin(t + radius_frac * 10) * 4) * 16), int(span * 16))
        if scan_y is not None:
            p.setPen(QPen(theme.qcolor(theme.PALE, 0.9), 2))
            p.drawLine(QPointF(-R * 0.7, scan_y), QPointF(R * 0.7, scan_y))
            p.fillRect(QRectF(-R * 0.7, scan_y - 10, R * 1.4, 10), theme.qcolor(color, 0.12))

        # granted: expanding rings
        if self.mode == "granted":
            for k in range(3):
                age = (since * 0.9 + k / 3) % 1.0
                p.setPen(QPen(theme.qcolor(color, (1 - age) * 0.7), 2))
                p.drawEllipse(QPointF(0, 0), R * (0.4 + age * 0.9), R * (0.4 + age * 0.9))
        p.resetTransform()

        # labels
        font = QFont("Consolas", 14)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 5)
        p.setFont(font)
        p.setPen(theme.qcolor(color, flicker))
        dots = "." * (int(t * 3) % 4) if self.mode == "scanning" else ""
        p.drawText(QRectF(0, cy + R * 1.18, w, 28), Qt.AlignmentFlag.AlignHCenter, MODE_TEXT[self.mode] + dots)
        if self.subtitle:
            small = QFont("Consolas", 9)
            small.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2)
            p.setFont(small)
            p.setPen(theme.qcolor(theme.MUTED))
            p.drawText(QRectF(10, cy + R * 1.18 + 30, w - 20, 20), Qt.AlignmentFlag.AlignHCenter, self.subtitle.upper())
        p.end()
