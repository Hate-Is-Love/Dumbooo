"""The audio-reactive 3D point blob at the centre of DUMBOO's core.

A sphere of ~1,400 glowing dots (Fibonacci lattice), deformed by smooth layered 3D waves,
slowly rotating and drawn in perspective, like a living cloud of light. Colours follow
the current phase: the top takes the state colour (cyan when listening, violet when
thinking …) and blends into violet/pink underneath, as in DUMBOO's theme.

* **Voice reactive:** the deformation swells with the real microphone level (your voice)
  and with DUMBOO's own voice while it speaks, scaled by the user's *sensitivity*.
* **Interactive:** drag to spin it (with inertia), hover to make the surface ripple under
  the cursor; clicks are handled by ``CoreWidget`` (click = talk).
* **Cheap:** positions are computed with numpy in one go and every dot is splatted into one
  RGBA image with ``np.bincount`` (additive glow), drawn with a single ``drawImage``.
"""

from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage, QPainter

def _fibonacci_sphere(count: int) -> np.ndarray:
    i = np.arange(count, dtype=np.float64) + 0.5
    phi = np.arccos(1.0 - 2.0 * i / count)
    theta = math.pi * (1.0 + 5.0 ** 0.5) * i
    return np.stack([np.cos(theta) * np.sin(phi), np.cos(phi), np.sin(theta) * np.sin(phi)], axis=1)


class PointBlob:
    def __init__(self, count: int = 2400) -> None:
        self.base = _fibonacci_sphere(count)
        self.yaw = 0.0
        self.pitch = 0.32
        self.spin = 0.0              # extra yaw speed from dragging (decays: inertia)
        self.amp = 0.06              # current deformation (eased)
        self.ripple = 0.0            # hover ripple strength (decays)
        self.ripple_at = (0.0, 0.0)  # cursor position, in blob radii from the centre
        self.time = 0.0

    # -- inputs --------------------------------------------------------------------------------
    def drag(self, dx: float, dy: float) -> None:
        self.yaw += dx * 0.012
        self.pitch = max(-1.2, min(1.2, self.pitch + dy * 0.008))
        self.spin = dx * 0.6

    def hover(self, x: float, y: float) -> None:
        self.ripple_at = (x, y)
        self.ripple = min(1.0, self.ripple + 0.35)

    def advance(self, dt: float, speed: float, target_amp: float) -> None:
        """``speed``: how fast the surface flows; ``target_amp``: how strongly it deforms (0..~0.5)."""
        self.time += dt * (0.6 + 0.9 * speed)
        self.yaw += dt * (0.18 + 0.12 * speed) + self.spin * dt
        self.spin *= max(0.0, 1.0 - dt * 1.8)
        rise = 16.0 if target_amp > self.amp else 4.0   # react fast to a louder voice, relax slowly
        self.amp += (target_amp - self.amp) * min(1.0, dt * rise)
        self.ripple *= max(0.0, 1.0 - dt * 1.5)

    # -- geometry ---------------------------------------------------------------------------------
    def points(self, radius: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Screen x, screen y (centred on 0, 0) and depth 0 (back) .. 1 (front)."""
        p, t = self.base, self.time
        wave = (np.sin(p[:, 0] * 2.3 + t * 1.25) * np.cos(p[:, 1] * 2.9 - t * 0.85)
                + 0.65 * np.sin(p[:, 2] * 3.4 + t * 1.6 + p[:, 0] * 1.5)
                + 0.40 * np.sin((p[:, 0] + p[:, 1]) * 4.6 - t * 2.2)
                + 0.25 * np.cos(p[:, 1] * 6.0 + p[:, 2] * 2.0 + t * 2.9)) / 2.3
        r = 1.0 + self.amp * wave
        pts = p * r[:, None]
        cy, sy = math.cos(self.yaw), math.sin(self.yaw)
        cp, sp = math.cos(self.pitch), math.sin(self.pitch)
        x = pts[:, 0] * cy + pts[:, 2] * sy
        z = -pts[:, 0] * sy + pts[:, 2] * cy
        y = pts[:, 1] * cp - z * sp
        z = pts[:, 1] * sp + z * cp
        if self.ripple > 0.01:  # a ripple travelling outwards from the cursor (front side only)
            rx, ry = self.ripple_at
            d = np.sqrt((x - rx) ** 2 + (y + ry) ** 2)
            bump = 1.0 + self.ripple * 0.10 * np.exp(-d * d * 4.0) * np.sin(self.time * 14.0 - d * 12.0) * (z > 0)
            x, y, z = x * bump, y * bump, z * bump
        camera = 3.2
        s = camera / (camera - z)
        depth = np.clip((z + 1.3) / 2.6, 0.0, 1.0)
        return x * s * radius, -y * s * radius, depth

    # -- painting ------------------------------------------------------------------------------------
    def render(self, radius: float, top: QColor, bottom: QColor, brightness: float) -> QImage:
        """Splat every dot into an RGBA image with numpy (additive glow): one image per frame, no per-dot
        Python objects. About 4x cheaper than drawing the points one by one with QPainter."""
        xs, ys, depth = self.points(radius)
        size = int(radius * 2.7) | 1
        half = size // 2
        vertical = np.clip((ys / max(radius, 1.0) + 1.15) / 2.3, 0.0, 1.0)   # 0 top .. 1 bottom
        rgb_top = np.array([top.redF(), top.greenF(), top.blueF()])
        rgb_bottom = np.array([bottom.redF(), bottom.greenF(), bottom.blueF()])
        colour = rgb_top[None, :] + (rgb_bottom - rgb_top)[None, :] * vertical[:, None]   # (N, 3)
        alpha = np.clip((0.30 + 0.85 * depth) * brightness, 0.08, 1.0)
        px = np.rint(xs).astype(np.int64) + half
        py = np.rint(ys).astype(np.int64) + half
        big = depth > 0.55                      # front dots are drawn larger
        acc = np.zeros((4, size * size))
        for (dx, dy), w in _KERNEL:
            weight = alpha * np.where(big, w[1], w[0])
            keep = (weight > 0) & (px + dx >= 0) & (px + dx < size) & (py + dy >= 0) & (py + dy < size)
            idx = (py[keep] + dy) * size + (px[keep] + dx)
            wk = weight[keep]
            for c in range(3):
                acc[c] += np.bincount(idx, colour[keep, c] * wk, minlength=size * size)
            acc[3] += np.bincount(idx, wk, minlength=size * size)
        a = np.clip(acc[3], 0.0, 1.0)
        rgb = np.clip(acc[:3], 0.0, 1.0)        # additive light, already premultiplied
        pixels = np.empty((size * size, 4), dtype=np.uint8)
        pixels[:, 0:3] = (rgb.T * 255).astype(np.uint8)
        pixels[:, 3] = (np.maximum(a, rgb.max(axis=0)) * 255).astype(np.uint8)
        image = QImage(pixels.tobytes(), size, size, size * 4, QImage.Format.Format_RGBA8888_Premultiplied)
        return image.copy()                     # own the pixels (the numpy buffer is freed after return)

    def paint(self, p: QPainter, radius: float, top: QColor, bottom: QColor, brightness: float) -> None:
        dpr = max(1.0, float(p.device().devicePixelRatioF())) if p.device() is not None else 1.0
        image = self.render(radius * dpr, top, bottom, brightness)   # sharp on scaled (125 %, 150 %) displays
        image.setDevicePixelRatio(dpr)
        p.drawImage(QPointF(-image.width() / (2 * dpr), -image.height() / (2 * dpr)), image)


# Splat kernel: (offset, (weight for back dots, weight for front dots)) — a small soft round dot.
_KERNEL = [((0, 0), (0.95, 1.0)),
           ((1, 0), (0.30, 0.65)), ((-1, 0), (0.30, 0.65)), ((0, 1), (0.30, 0.65)), ((0, -1), (0.30, 0.65)),
           ((1, 1), (0.08, 0.30)), ((-1, 1), (0.08, 0.30)), ((1, -1), (0.08, 0.30)), ((-1, -1), (0.08, 0.30)),
           ((2, 0), (0.0, 0.08)), ((-2, 0), (0.0, 0.08)), ((0, 2), (0.0, 0.08)), ((0, -2), (0.0, 0.08))]
