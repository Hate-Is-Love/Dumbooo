"""The animated DUMBOO core (QPainter, no external assets).

Visual language follows the reference HUD video: a radial "reactor core"
built from dotted orbit rings, a tick ring, segmented arcs, scattered
particles, symmetric blades around a glowing orange centre, and solid cyan
callout tags connected by leader lines.

Conversation visuals (also from the reference video):

* While DUMBOO is thinking (PROCESSING) and receiving an answer (STREAMING),
  the rings tilt and separate along a diagonal axis into the "exploded
  reactor" view, with orange coils at both ends. They reassemble into the front
  view when it starts speaking.
* An "ANALYSING DATA" / "RECEIVING DATA" panel shows real numbers only:
  provider, model, elapsed time, characters received, context turns.
* Every streamed piece of the answer sends a burst of data particles into the
  core, sized by the real number of characters. Sending a message and finishing
  an answer emit a pulse ring.

Module 3 visuals: while saved memories and history are searched for the
current question (``memory_scan``), violet data particles stream in from the
left and the panel reads RETRIEVING MEMORY; the number of facts found is
shown and sent into the core as a burst. ``flash`` shows short real-event
tags such as IDENTITY CONFIRMED, MEMORY WRITTEN or MEMORY ERASED.

Centre: an audio-reactive 3D point blob (``dashboard/blob.py``) in the phase colours. It
swells with the real voice level (yours while listening, DUMBOO's while speaking) times the
user's sensitivity, churns while thinking, and is interactive: drag to spin, hover to ripple,
click to talk (``talk_requested``), right-click for sensitivity. With the blob switched off
the original glowing orb is drawn instead.

The widget is driven only by ``set_state`` (from the central state machine),
``set_level`` (real microphone or playback amplitude) and the conversation
hooks ``on_message_sent`` / ``on_answer_piece`` / ``on_answer_done``. It never
decides anything itself. Every animated property eases towards a per-state target,
so transitions are smooth. Cost is kept low: points and lines are drawn in
batched calls, the frame rate drops to 12 fps while sleeping, and the timer
stops completely while the window is hidden.
"""

from __future__ import annotations

import math
import random
from contextlib import contextmanager

from PySide6.QtCore import QElapsedTimer, QLineF, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QFont, QLinearGradient, QPainter, QPainterPath, QPixmap,
                           QPen, QPolygonF, QRadialGradient)
from PySide6.QtWidgets import QMenu, QSizePolicy, QWidget

from dashboard.blob import PointBlob
from dashboard.styles import theme

TAU = math.tau

# Per-state animation targets.
#   energy: glow brightness   speed: rotation multiplier   scale: overall size
#   jitter: particle agitation   sweep: processing scanner on/off
STATE_TARGETS = {
    "SLEEPING":      dict(energy=0.12, speed=0.15, scale=0.86, jitter=0.0, sweep=0.0, tilt=0.0, fps=12),
    "WAKING":        dict(energy=1.00, speed=2.20, scale=1.00, jitter=0.6, sweep=0.0, tilt=0.0, fps=30),
    "IDLE":          dict(energy=0.55, speed=0.55, scale=1.00, jitter=0.1, sweep=0.0, tilt=0.0, fps=20),
    "LISTENING":     dict(energy=0.80, speed=0.90, scale=1.02, jitter=0.4, sweep=0.0, tilt=0.0, fps=30),
    "PROCESSING":    dict(energy=0.70, speed=2.40, scale=0.92, jitter=0.3, sweep=1.0, tilt=1.0, fps=24),
    "STREAMING":     dict(energy=0.85, speed=1.60, scale=0.94, jitter=0.5, sweep=0.35, tilt=0.75, fps=24),
    "SPEAKING":      dict(energy=0.75, speed=0.90, scale=1.02, jitter=0.4, sweep=0.0, tilt=0.0, fps=30),
    "INTERRUPTED":   dict(energy=0.95, speed=0.25, scale=0.96, jitter=0.8, sweep=0.0, tilt=0.0, fps=30),
    "ERROR":         dict(energy=0.60, speed=0.30, scale=0.97, jitter=0.2, sweep=0.0, tilt=0.0, fps=30),
    "SHUTTING_DOWN": dict(energy=0.05, speed=0.05, scale=0.80, jitter=0.0, sweep=0.0, tilt=0.0, fps=12),
}

# How much the blob deforms in each phase without any voice (it breathes at rest, churns while thinking).
BLOB_BASE_AMP = {"SLEEPING": 0.03, "WAKING": 0.16, "IDLE": 0.055, "LISTENING": 0.07, "PROCESSING": 0.15,
                 "STREAMING": 0.11, "SPEAKING": 0.07, "INTERRUPTED": 0.10, "ERROR": 0.12, "SHUTTING_DOWN": 0.02}
BLOB_SENSITIVITY_PRESETS = (("Low", 0.5), ("Normal", 1.0), ("High", 1.6), ("Very high", 2.4))

STATE_CAPTIONS = {
    "SLEEPING": "standby · say “Hey DUMBOO”",
    "WAKING": "initialising",
    "IDLE": "ready · say “Hey DUMBOO”, press TALK or type",
    "LISTENING": "listening…",
    "PROCESSING": "thinking",
    "STREAMING": "receiving answer",
    "SPEAKING": "speaking · talk to interrupt",
    "INTERRUPTED": "interrupted · listening",
    "ERROR": "error",
    "SHUTTING_DOWN": "shutting down",
}


def _ease(current: float, target: float, rate: float) -> float:
    return current + (target - current) * rate


class _LevelMeter:
    """Automatic gain: maps a raw RMS to 0..1 against the recent noise floor and loudest peak.

    A quiet laptop microphone (speech around 0.005 RMS after noise suppression) and loud speech
    playback both move the blob fully, while steady background noise stays near zero.
    """

    def __init__(self) -> None:
        self.floor = -60.0
        self.peak = -30.0

    def __call__(self, rms: float) -> float:
        db = max(-75.0, 20 * math.log10(max(rms, 1e-6)))
        self.floor += (db - self.floor) * (0.3 if db < self.floor else 0.015)   # follows quiet fast, noise slowly
        self.peak += (db - self.peak) * (0.5 if db > self.peak else 0.01)       # follows loud fast, relaxes slowly
        relative = (db - self.floor - 3) / max(18.0, self.peak - self.floor)
        absolute = (db + 50) / 38   # the fixed scale: loud input always counts fully
        return max(0.0, min(1.0, max(relative, absolute)))


class CoreWidget(QWidget):
    talk_requested = Signal()                      # the blob was clicked
    blob_settings_changed = Signal(bool, float)    # (enabled, sensitivity) chosen from the right-click menu

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._blob = PointBlob()
        self._blob_on = True
        self._blob_sensitivity = 1.0
        self._blob_center = QPointF(0, 0)
        self._blob_radius = 0.0
        self._press: QPointF | None = None
        self._dragged = False
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(360, 360)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self._state = "SLEEPING"
        self._caption = STATE_CAPTIONS["SLEEPING"]
        self._detail = ""
        self._intensity = 0.8
        self._level_target = 0.0
        self._level = 0.0
        self._level_hold = 0.0
        self._meters = {"voice": _LevelMeter(), "bot": _LevelMeter()}
        self._tags: list[tuple[str, str]] = []
        targets = STATE_TARGETS["SLEEPING"]
        self._energy = targets["energy"]
        self._speed = targets["speed"]
        self._scale = 0.4
        self._jitter = 0.0
        self._sweep = 0.0
        self._flash = 0.0
        line, energy = theme.STATE_COLORS["SLEEPING"]
        self._line = QColor(line)
        self._energy_color = QColor(energy)
        self._phase = 0.0
        self._rng = random.Random(7)
        self._wave = [0.0] * 72
        self._tilt = 0.0
        self._panel = 0.0
        self._inflow: list[list[float]] = []    # [angle, progress 0..1, speed, size]
        self._pulses: list[list[float]] = []    # [age s, strength]
        self._trace: list[int] = []             # characters per streamed piece (real data)
        self._ai = {"provider": "", "model": "", "turns": "0"}
        self._sent_ms: int | None = None
        self._done_ms: int | None = None
        self._received = 0
        self._mem_scan = False
        self._mem_found: tuple[int, int] | None = None
        self._flash_text = ""
        self._flash_color = theme.CYAN
        self._flash_until = 0
        self._task: dict | None = None          # running automation task (real step states)
        self._task_until = 0
        self._build_geometry()
        self._clock = QElapsedTimer()
        self._clock.start()
        self._last_ms = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)
        self._timer.setInterval(1000 // 30)  # started by showEvent, so a hidden dashboard costs nothing

    # -- inputs ------------------------------------------------------------------
    def set_state(self, state: str, detail: str = "") -> None:
        if state not in STATE_TARGETS:
            return
        if state != self._state and state in ("WAKING", "INTERRUPTED", "ERROR"):
            self._flash = 1.0
        if state == "WAKING" and self._state == "SLEEPING":
            self._scale = 0.35  # expand outwards during the wake-up animation
        self._state = state
        self._caption = STATE_CAPTIONS.get(state, "")
        self._detail = detail
        self._timer.setInterval(int(1000 / self._fps()))
        self.update()

    def state(self) -> str:
        return self._state

    def set_level(self, rms: float, source: str = "voice") -> None:
        """``source``: "voice" (the microphone) or "bot" (DUMBOO's own speech playback)."""
        self._level_target = self._meters[source](rms)
        self._level_hold = 0.22  # levels arrive at 10-20 Hz: hold each one instead of decaying between them

    def set_blob(self, enabled: bool, sensitivity: float) -> None:
        self._blob_on = bool(enabled)
        self._blob_sensitivity = max(0.0, min(3.0, float(sensitivity)))
        self.setToolTip("Click to talk · drag to spin · right-click for blob sensitivity" if self._blob_on else "")
        self.update()

    def blob_settings(self) -> tuple[bool, float]:
        return self._blob_on, self._blob_sensitivity

    def set_intensity(self, value: float) -> None:
        self._intensity = max(0.0, min(1.0, value))
        self._build_geometry()
        self._timer.setInterval(int(1000 / self._fps()))

    def set_tags(self, tags: list[tuple[str, str]]) -> None:
        self._tags = tags[:5]
        self.update()

    # -- conversation hooks (all driven by real events) --------------------------------
    def set_ai_info(self, provider: str, model: str, turns: int) -> None:
        self._ai = {"provider": provider, "model": model, "turns": str(turns)}

    def on_message_sent(self) -> None:
        self._sent_ms = self._clock.elapsed()
        self._done_ms = None
        self._received = 0
        self._trace = []
        self._pulses.append([0.0, 1.0])

    def on_answer_piece(self, chars: int) -> None:
        self._blob.amp = min(0.45, self._blob.amp + 0.004 * min(chars, 40))  # each streamed piece pulses the blob
        self._received += chars
        self._trace = (self._trace + [chars])[-48:]
        if self._intensity <= 0.01:
            return
        burst = min(14, 2 + chars // 6)
        for _ in range(burst):
            # particles enter from the right, where the callout tags are
            angle = self._rng.uniform(-0.9, 0.9)
            self._inflow.append([angle, 0.0, self._rng.uniform(0.9, 1.6), self._rng.uniform(1.2, 2.6), 0])
        del self._inflow[:-220]

    def on_answer_done(self) -> None:
        if self._sent_ms is not None and self._done_ms is None:
            self._done_ms = self._clock.elapsed()
        self._pulses.append([0.0, 0.6])

    def memory_scan(self, active: bool, memories: int = 0, messages: int = 0) -> None:
        """Real retrieval activity from the database (start / done with counts)."""
        self._mem_scan = active
        if active:
            self._mem_found = None
            return
        self._mem_found = (memories, messages)
        for _ in range(min(24, 4 + 4 * (memories + messages))):
            angle = 3.14159 + self._rng.uniform(-0.8, 0.8)
            self._inflow.append([angle, 0.0, self._rng.uniform(1.0, 1.7), 2.0, 1])
        self.update()

    def automation_task(self, task: dict | None) -> None:
        """A Module 4 task snapshot: one ring segment per step, coloured by its real status."""
        if task is None:
            self._task = None
            return
        previous = self._task
        self._task = task
        finished = task["status"] in ("completed", "partially_completed", "failed", "cancelled")
        self._task_until = self._clock.elapsed() + (3500 if finished else 10**9)
        done_now = sum(1 for s in task["steps"] if s["status"] == "completed")
        done_before = sum(1 for s in (previous or {}).get("steps", []) if s["status"] == "completed")             if previous and previous.get("id") == task.get("id") else 0
        if done_now > done_before:
            self._pulses.append([0.0, 0.8])          # a verified step: pulse outwards
            for _ in range(10):
                angle = self._rng.uniform(0, 6.283)
                self._inflow.append([angle, 0.0, self._rng.uniform(1.0, 1.6), 2.2, 0])
        self.update()

    def flash(self, text: str, color: str = theme.CYAN, ms: int = 1800) -> None:
        """Show a short status tag over the core plus a pulse ring (for real events only)."""
        self._flash_text = text.upper()
        self._flash_color = color
        self._flash_until = self._clock.elapsed() + ms
        self._pulses.append([0.0, 0.9])
        self.update()

    def _elapsed_s(self) -> float | None:
        if self._sent_ms is None:
            return None
        end = self._done_ms if self._done_ms is not None else self._clock.elapsed()
        return max(0.0, (end - self._sent_ms) / 1000)

    def _fps(self) -> float:
        fps = STATE_TARGETS[self._state]["fps"]
        if self._intensity < 0.15:
            fps = min(fps, 10)
        return fps

    # -- geometry (random but stable) ----------------------------------------------
    def _build_geometry(self) -> None:
        rng = random.Random(42)
        count = int(40 + 180 * self._intensity)
        self._particles = [
            (rng.uniform(0, TAU), rng.uniform(0.5, 1.22), rng.uniform(0.02, 0.12) * rng.choice((-1, 1)),
             rng.uniform(0.15, 0.9), rng.uniform(0, TAU))
            for _ in range(count)
        ]
        self._arcs_outer = [(rng.uniform(0, 360), rng.uniform(8, 50), rng.choice((1.5, 2.5, 4.0))) for _ in range(9)]
        self._arcs_inner = [(i * 45 + rng.uniform(-6, 6), rng.uniform(18, 32), 3.0) for i in range(8)]
        self._nodes = [(rng.uniform(0, TAU), rng.uniform(0.9, 1.1)) for _ in range(14)]

    # -- animation step ---------------------------------------------------------------
    def _advance(self) -> None:
        now = self._clock.elapsed()
        dt = min(0.1, (now - self._last_ms) / 1000.0) if self._last_ms else 1 / 30
        self._last_ms = now
        t = STATE_TARGETS[self._state]
        k = min(1.0, dt * 4.0)
        self._energy = _ease(self._energy, t["energy"], k)
        self._speed = _ease(self._speed, t["speed"], k)
        self._scale = _ease(self._scale, t["scale"], min(1.0, dt * (5.0 if self._state == "WAKING" else 2.0)))
        self._jitter = _ease(self._jitter, t["jitter"], k)
        self._sweep = _ease(self._sweep, t["sweep"], k)
        self._tilt = _ease(self._tilt, t["tilt"] * (0.4 + 0.6 * self._intensity), min(1.0, dt * 2.5))
        busy = self._state in ("PROCESSING", "STREAMING", "SPEAKING")
        self._panel = _ease(self._panel, 1.0 if busy else 0.0, min(1.0, dt * 5))
        if self._mem_scan and self._intensity > 0.01:
            for _ in range(2):  # continuous violet stream from the left while the database is searched
                angle = 3.14159 + self._rng.uniform(-0.9, 0.9)
                self._inflow.append([angle, 0.0, self._rng.uniform(0.8, 1.4), 1.6, 1])
            del self._inflow[:-260]
        for particle in self._inflow:
            particle[1] += dt * particle[2]
        self._inflow = [q for q in self._inflow if q[1] < 1.0]
        for pulse in self._pulses:
            pulse[0] += dt
        self._pulses = [q for q in self._pulses if q[0] < 1.1]
        self._flash = max(0.0, self._flash - dt * 1.6)
        line, energy = theme.STATE_COLORS[self._state]
        self._line = theme.mix(self._line, QColor(line), k)
        self._energy_color = theme.mix(self._energy_color, QColor(energy), k)
        reactive = self._state in ("LISTENING", "SPEAKING", "INTERRUPTED", "WAKING")
        target_level = self._level_target if reactive else 0.0
        self._level = _ease(self._level, target_level, min(1.0, dt * (14 if target_level > self._level else 10)))
        self._level_hold -= dt
        if self._level_hold <= 0:
            self._level_target *= math.exp(-dt * 8)  # decays once level events stop
        if self._blob_on:
            voice = self._level * self._blob_sensitivity * 0.42
            self._blob.advance(dt * (0.25 + 0.75 * self._intensity), self._speed,
                               BLOB_BASE_AMP.get(self._state, 0.06) + voice)
        self._phase += dt * self._speed * (0.25 + 0.75 * self._intensity)
        # waveform: travelling sines plus smoothed noise, scaled by the real level
        amp = self._level
        for i in range(len(self._wave)):
            noise = self._rng.uniform(-1, 1) * amp
            self._wave[i] = self._wave[i] * 0.7 + noise * 0.3
        self.update()

    # -- painting --------------------------------------------------------------------------
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2 - 18
        base = min(w * 0.62, h - 90) / 2
        R = base * self._scale * 0.86
        e = min(1.0, self._energy + self._flash * 0.5)
        line = self._line
        flicker = 1.0
        if self._state == "ERROR":
            flicker = 0.75 + 0.25 * math.sin(self._phase * 40)

        self._paint_backdrop(p, cx, cy, base, w, h)
        p.translate(cx, cy)
        self._paint_particles(p, R, line, e)
        self._paint_pulses(p, R, line)
        layer = self._layer
        with layer(p, R, 0.0):
            self._paint_static_rings(p, R, line, e * flicker)
        if self._tilt > 0.05:
            self._paint_coils(p, R, e)
        with layer(p, R, 1.0):
            self._paint_dotted_ring(p, R * 0.99, self._phase * 0.25, 180, line, e)
        with layer(p, R, 0.62):
            self._paint_ticks(p, R * 0.82, -self._phase * 0.4, line, e)
        with layer(p, R, 0.35):
            self._paint_arcs(p, R * 0.90, self._arcs_outer, self._phase * 0.3, line, e * 0.9)
        with layer(p, R, -0.35):
            self._paint_arcs(p, R * 0.72, self._arcs_inner, -self._phase * 0.55, line, e)
        with layer(p, R, -0.12):
            self._paint_wave(p, R * 0.60, R, line, e)
        with layer(p, R, -0.62):
            self._paint_dotted_ring(p, R * 0.50, -self._phase * 0.8, 96, line, e * 0.9)
        with layer(p, R, 0.0):
            if self._blob_on:
                self._paint_blob(p, cx, cy, R, e)  # the blob replaces the orb and the blades: an uncluttered centre
            else:
                self._paint_orb(p, R, e)
                self._paint_blades(p, R, line, e)
            if self._sweep > 0.02:
                self._paint_sweep(p, R, line)
        self._paint_inflow(p, R, line)
        p.resetTransform()
        self._paint_tags(p, cx, cy, R, line, e)
        self._paint_analysis(p, cx, cy, R, w)
        self._paint_task(p, cx, cy, R, w)
        self._paint_flash(p, cx, cy, R, w)
        self._paint_labels(p, w, h, cy, R)
        p.end()

    def _paint_flash(self, p: QPainter, cx: float, cy: float, R: float, w: int) -> None:
        remaining = self._flash_until - self._clock.elapsed()
        if remaining <= 0 or not self._flash_text:
            return
        alpha = min(1.0, remaining / 400)
        font = QFont("Consolas", 11)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 4)
        p.setFont(font)
        width = min(w - 20, 60 + 13 * len(self._flash_text))
        box = QRectF(cx - width / 2, cy - R * 1.12 - 34, width, 28)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.qcolor(self._flash_color, 0.22 * alpha))
        p.drawRect(box)
        p.setPen(QPen(theme.qcolor(self._flash_color, alpha), 1.4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(box)
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, self._flash_text)

    # -- exploded "reactor" view ------------------------------------------------------------
    AXIS_DEG = -35.0  # lower-left -> upper-right, as in the reference video

    @contextmanager
    def _layer(self, p: QPainter, R: float, offset: float):
        """Draw one ring layer, tilted and pushed along the axis by the current tilt amount."""
        p.save()
        if self._tilt > 0.01:
            a = math.radians(self.AXIS_DEG)
            shift = offset * R * 0.62 * self._tilt
            p.translate(math.cos(a) * shift, math.sin(a) * shift)
            p.rotate(self.AXIS_DEG)
            p.scale(1.0 - 0.7 * self._tilt, 1.0)
            p.rotate(-self.AXIS_DEG)
        try:
            yield
        finally:
            p.restore()

    def _paint_coils(self, p: QPainter, R: float, e: float) -> None:
        """Orange coil cylinders at both ends of the exploded axis."""
        a = math.radians(self.AXIS_DEG)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for end in (1.3, -1.25):
            for i in range(9):
                shift = (end + (i - 4) * 0.035) * R * 0.62 * self._tilt
                with self._layer(p, R, 0.0):
                    p.translate(math.cos(a) * shift, math.sin(a) * shift)
                    alpha = (0.25 + 0.55 * e) * self._tilt
                    p.setPen(QPen(theme.qcolor(theme.ORANGE if i % 3 else theme.AMBER, alpha), 1.3))
                    r = R * (0.13 if i % 4 else 0.16)
                    p.drawEllipse(QPointF(0, 0), r, r)

    # -- conversation effects ---------------------------------------------------------------
    def _paint_inflow(self, p: QPainter, R: float, line: QColor) -> None:
        if not self._inflow:
            return
        groups = {0: (QPolygonF(), [], theme.CYAN, "#e8ffff"), 1: (QPolygonF(), [], theme.VIOLET, "#e6e0ff")}
        for angle, progress, _speed, _size, kind in self._inflow:
            heads, trails, _c, _h = groups[kind]
            ease = progress * progress  # accelerate into the core
            c, s_ = math.cos(angle), math.sin(angle) * 0.85
            r = R * (1.55 - 1.5 * ease)
            tail = R * (1.55 - 1.5 * max(0.0, ease - 0.08))
            heads.append(QPointF(c * r, s_ * r))
            trails.append(QLineF(c * tail, s_ * tail, c * r, s_ * r))
        for heads, trails, trail_color, head_color in groups.values():
            if not trails:
                continue
            p.setPen(QPen(theme.qcolor(trail_color, 0.3), 1))
            p.drawLines(trails)  # one batched call instead of one per particle
            p.setPen(QPen(theme.qcolor(head_color, 0.85), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap))
            p.drawPoints(heads)

    def _paint_pulses(self, p: QPainter, R: float, line: QColor) -> None:
        p.setBrush(Qt.BrushStyle.NoBrush)
        for age, strength in self._pulses:
            t = age / 1.1
            radius = R * (0.35 + 1.1 * t)
            p.setPen(QPen(theme.qcolor(line.name(), strength * (1 - t) * 0.8), 2.0 + 3 * (1 - t)))
            p.drawEllipse(QPointF(0, 0), radius, radius)

    def _paint_task(self, p: QPainter, cx: float, cy: float, R: float, w: int) -> None:
        """Automation: segmented step ring around the core and an 'EXECUTING TASK' read-out."""
        task = self._task
        if task is None or self._clock.elapsed() > self._task_until:
            return
        steps = task["steps"] or [{"status": "pending"}]
        n = len(steps)
        t = self._clock.elapsed() / 1000
        colors = {"completed": theme.GREEN, "failed": theme.RED, "denied": theme.RED, "unverified": theme.AMBER,
                  "cancelled": theme.MUTED, "skipped": theme.MUTED, "running": theme.CYAN,
                  "awaiting_confirmation": theme.AMBER, "pending": theme.LINE_BRIGHT}
        ring = R * 1.13
        rect = QRectF(cx - ring, cy - ring, 2 * ring, 2 * ring)
        gap = 6 if n > 1 else 0
        span = 360 / n
        for i, step in enumerate(steps):
            status = step["status"]
            color = colors.get(status, theme.LINE_BRIGHT)
            alpha = 0.95
            if status in ("running", "awaiting_confirmation"):
                alpha = 0.45 + 0.55 * abs(math.sin(t * (5 if status == "running" else 3)))
            p.setPen(QPen(theme.qcolor(color, alpha), 4.5 if status != "pending" else 2.0, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.FlatCap))
            start = 90 - i * span - gap / 2
            p.drawArc(rect, int(start * 16), int(-(span - gap) * 16))
        running = next((s for s in steps if s["status"] == "running"), None)
        if running is not None:  # a bright head sweeping along the running segment
            i = steps.index(running)
            head = 90 - i * span - gap / 2 - ((t * 120) % max(1, span - gap))
            p.setPen(QPen(theme.qcolor(theme.PALE, 0.95), 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(rect, int(head * 16), int(-6 * 16))
        if w < 620:
            return
        done = sum(1 for s in steps if s["status"] == "completed")
        status = task["status"]
        title = {"awaiting_confirmation": "AWAITING CONFIRMATION", "completed": "TASK VERIFIED",
                 "partially_completed": "PARTIALLY COMPLETED", "failed": "TASK FAILED",
                 "cancelled": "TASK CANCELLED"}.get(status, task.get("label") or "EXECUTING TASK")
        if task.get("label") == "RESEARCHING" and status == "completed":
            title = "RESEARCH COMPLETE"
        accent = {"awaiting_confirmation": theme.AMBER, "completed": theme.GREEN, "failed": theme.RED,
                  "partially_completed": theme.AMBER, "cancelled": theme.MUTED}.get(status, theme.CYAN)
        x = min(w - 250.0, cx + R * 1.3)
        y = cy + R * 0.55
        box = QRectF(x, y, 240, 34)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.qcolor("#123238", 0.92))
        p.drawRect(box)
        p.setBrush(theme.qcolor(accent, 1.0))
        p.drawRect(QRectF(x, y, 4, 34))
        font = QFont("Consolas", 11)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2)
        p.setFont(font)
        p.setPen(theme.qcolor(accent, 1.0))
        p.drawText(box.adjusted(14, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter, title)
        small = QFont("Consolas", 8)
        p.setFont(small)
        current = running or next((s for s in steps if s["status"] == "awaiting_confirmation"), None)
        rows = [("STEPS", f"{done}/{n} verified"), ("TASK", (task.get("title") or "")[:26])]
        if current is not None:
            rows.append(("NOW", (current.get("description") or current.get("capability", ""))[:26]))
            if current.get("risk"):
                rows.append(("RISK", current["risk"].upper()))
        for i, (key, value) in enumerate(rows):
            ry = y + 50 + i * 15
            p.setPen(theme.qcolor(theme.MUTED, 1.0))
            p.drawText(QPointF(x + 2, ry), key)
            p.setPen(theme.qcolor(theme.PALE, 1.0))
            p.drawText(QPointF(x + 62, ry), value)

    def _paint_analysis(self, p: QPainter, cx: float, cy: float, R: float, w: int) -> None:
        """'ANALYSING DATA' panel (reference video), showing only real numbers."""
        if self._panel < 0.02 or w < 620:
            return
        alpha = self._panel
        title = {"PROCESSING": "ANALYSING DATA", "STREAMING": "RECEIVING DATA",
                 "SPEAKING": "TRANSMITTING VOICE"}.get(self._state, "ANALYSING DATA")
        if self._mem_scan:
            title = "RETRIEVING MEMORY"
        x = max(8.0, cx - R * 1.55 - 40)
        y = cy - R * 0.95
        box = QRectF(x, y, 230, 34)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.qcolor("#123238", 0.92 * alpha))
        p.drawRect(box)
        p.setBrush(theme.qcolor(theme.CYAN, alpha))
        p.drawRect(QRectF(x, y, 4, 34))
        font = QFont("Consolas", 12)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3)
        p.setFont(font)
        p.setPen(theme.qcolor(theme.CYAN, alpha))
        p.drawText(box.adjusted(14, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter, title)
        p.setPen(QPen(theme.qcolor(theme.PALE, 0.5 * alpha), 1))  # rule line like the reference header
        p.drawLine(QPointF(x + 20, y - 12), QPointF(x + 190, y - 12))
        small = QFont("Consolas", 8)
        small.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1)
        p.setFont(small)
        elapsed = self._elapsed_s()
        rows = [
            ("PROVIDER", (self._ai["provider"] or "—").upper()),
            ("MODEL", self._ai["model"][:22] or "—"),
            ("ELAPSED", f"{elapsed:5.2f} s" if elapsed is not None else "—"),
            ("RECEIVED", f"{self._received} chr"),
            ("CONTEXT", f"{self._ai['turns']} turns"),
            ("MEMORY", "searching…" if self._mem_scan else
             (f"{self._mem_found[0]} facts · {self._mem_found[1]} msgs" if self._mem_found else "—")),
        ]
        for i, (key, value) in enumerate(rows):
            ry = y + 50 + i * 15
            p.setPen(theme.qcolor(theme.MUTED, alpha))
            p.drawText(QPointF(x + 2, ry), key)
            p.setPen(theme.qcolor(theme.PALE, alpha))
            p.drawText(QPointF(x + 82, ry), value)
        if self._trace:  # one bar per streamed piece; height = its real size
            bx, by = x + 2, y + 50 + len(rows) * 15 + 8
            top = max(self._trace)
            for i, n in enumerate(self._trace):
                hgt = 2 + 16 * n / max(1, top)
                color = theme.ORANGE if i == len(self._trace) - 1 else theme.CYAN_DIM
                p.fillRect(QRectF(bx + i * 4.5, by + 18 - hgt, 3, hgt), theme.qcolor(color, 0.85 * alpha))

    def _paint_backdrop(self, p: QPainter, cx: float, cy: float, base: float, w: int, h: int) -> None:
        # The soft glow is rendered once per size into a pixmap, then drawn with the current energy as opacity.
        key = (w, h, round(cx), round(cy))
        if getattr(self, "_glow_key", None) != key:
            pixmap = QPixmap(w, h)
            pixmap.fill(Qt.GlobalColor.transparent)
            gp = QPainter(pixmap)
            glow = QRadialGradient(QPointF(cx, cy), base * 1.4)
            glow.setColorAt(0.0, theme.qcolor("#0f2a30", 0.77))
            glow.setColorAt(1.0, theme.qcolor(theme.BG, 0.0))
            gp.fillRect(pixmap.rect(), QBrush(glow))
            gp.end()
            self._glow, self._glow_key = pixmap, key
        p.setOpacity(min(1.0, 0.4 + self._energy) / 1.4)
        p.drawPixmap(0, 0, self._glow)
        p.setOpacity(1.0)
        # HUD crosshair with graduations
        pen = QPen(theme.qcolor(theme.LINE_BRIGHT, 0.35), 1)
        p.setPen(pen)
        p.drawLine(QPointF(cx - base * 1.35, cy), QPointF(cx - base * 1.08, cy))
        p.drawLine(QPointF(cx + base * 1.08, cy), QPointF(cx + base * 1.35, cy))
        p.drawLine(QPointF(cx, cy - base * 1.12), QPointF(cx, cy - base * 1.02))
        for i in range(10):
            x = cx - base * 1.35 + i * base * 0.027
            p.drawLine(QPointF(x, cy - 3), QPointF(x, cy + 3))
            x = cx + base * 1.08 + i * base * 0.027
            p.drawLine(QPointF(x, cy - 3), QPointF(x, cy + 3))

    def _paint_static_rings(self, p: QPainter, R: float, line: QColor, e: float) -> None:
        p.setBrush(Qt.BrushStyle.NoBrush)
        for radius, alpha, width in ((R * 1.08, 0.18, 1.0), (R * 0.94, 0.30, 1.0), (R * 0.78, 0.22, 1.0),
                                     (R * 0.56, 0.35, 1.2), (R * 0.42, 0.45, 1.0)):
            p.setPen(QPen(theme.qcolor(line.name(), alpha * (0.4 + e)), width))
            p.drawEllipse(QPointF(0, 0), radius, radius)

    def _paint_dotted_ring(self, p: QPainter, radius: float, rot: float, count: int, line: QColor, e: float) -> None:
        small, big = QPolygonF(), QPolygonF()
        for i in range(count):
            a = rot + TAU * i / count
            pt = QPointF(math.cos(a) * radius, math.sin(a) * radius)
            (big if i % 6 == 0 else small).append(pt)
        p.setPen(QPen(theme.qcolor(theme.PALE, 0.35 + 0.5 * e), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap))
        p.drawPoints(small)
        p.setPen(QPen(theme.qcolor(line.name(), 0.5 + 0.5 * e), 3.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawPoints(big)

    def _paint_ticks(self, p: QPainter, radius: float, rot: float, line: QColor, e: float) -> None:
        short, long_ = [], []
        n = 120
        for i in range(n):
            a = rot + TAU * i / n
            c, s = math.cos(a), math.sin(a)
            length = radius * (0.07 if i % 10 == 0 else 0.03)
            seg = QLineF(c * radius, s * radius, c * (radius - length), s * (radius - length))
            (long_ if i % 10 == 0 else short).append(seg)
        p.setPen(QPen(theme.qcolor(line.name(), 0.25 + 0.45 * e), 1))
        p.drawLines(short)
        p.setPen(QPen(theme.qcolor(theme.PALE, 0.4 + 0.5 * e), 1.6))
        p.drawLines(long_)

    def _paint_arcs(self, p: QPainter, radius: float, arcs, rot: float, line: QColor, e: float) -> None:
        rect = QRectF(-radius, -radius, radius * 2, radius * 2)
        deg = math.degrees(rot)
        for start, span, width in arcs:
            p.setPen(QPen(theme.qcolor(line.name(), 0.35 + 0.6 * e), width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.drawArc(rect, int((start + deg) * 16), int(span * 16))

    def _paint_particles(self, p: QPainter, R: float, line: QColor, e: float) -> None:
        if self._intensity <= 0.01:
            return
        buckets = [QPolygonF(), QPolygonF(), QPolygonF()]
        jitter = self._jitter * (0.4 + self._level)
        for angle, dist, speed, bright, seed in self._particles:
            a = angle + self._phase * speed * 3
            wobble = math.sin(self._phase * 3 + seed) * 0.025 * jitter
            r = R * (dist + wobble + self._level * 0.06 * math.sin(seed + self._phase * 7))
            idx = 0 if bright < 0.4 else (1 if bright < 0.7 else 2)
            buckets[idx].append(QPointF(math.cos(a) * r, math.sin(a) * r))
        for idx, poly in enumerate(buckets):
            alpha = (0.18, 0.4, 0.75)[idx] * (0.35 + 0.65 * e)
            color = theme.PALE if idx < 2 else line.name()
            cap = Qt.PenCapStyle.RoundCap if idx == 2 else Qt.PenCapStyle.SquareCap  # round caps only where visible
            p.setPen(QPen(theme.qcolor(color, alpha), (1.2, 1.8, 2.4)[idx], Qt.PenStyle.SolidLine, cap))
            p.drawPoints(poly)
        # a few "data nodes": small hollow circles on the outer field
        p.setPen(QPen(theme.qcolor(theme.PALE, 0.3 + 0.4 * e), 1))
        for angle, dist in self._nodes:
            a = angle + self._phase * 0.08
            p.drawEllipse(QPointF(math.cos(a) * R * dist * 1.04, math.sin(a) * R * dist * 1.04), 3.2, 3.2)

    def _paint_wave(self, p: QPainter, radius: float, R: float, line: QColor, e: float) -> None:
        n = len(self._wave)
        amp = R * 0.16
        poly = QPolygonF()
        for i in range(n + 1):
            j = i % n
            a = TAU * j / n
            travelling = math.sin(a * 6 + self._phase * 5) * 0.35 + math.sin(a * 11 - self._phase * 7) * 0.2
            offset = (self._wave[j] + travelling * self._level) * amp + math.sin(a * 3 + self._phase) * 1.5
            r = radius + offset
            poly.append(QPointF(math.cos(a) * r, math.sin(a) * r))
        color = self._energy_color if self._state == "SPEAKING" else line
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self._level > 0.15:  # the soft glow pass only while there is real audio to show
            p.setPen(QPen(theme.qcolor(color.name(), 0.10 + 0.25 * e), 3.5))
            p.drawPolyline(poly)
        p.setPen(QPen(theme.qcolor(color.name(), 0.5 + 0.5 * e), 1.4))
        p.drawPolyline(poly)

    def _paint_blob(self, p: QPainter, cx: float, cy: float, R: float, e: float) -> None:
        radius = R * 0.43  # inside the inner dotted ring (0.50 R), even at full swell
        self._blob_center, self._blob_radius = QPointF(cx, cy), radius * 1.2
        # soft glow behind the blob in the phase's energy colour
        glow = QRadialGradient(QPointF(0, 0), radius * 1.55)
        glow.setColorAt(0.0, theme.qcolor(self._energy_color.name(), 0.18 * e + 0.04))
        glow.setColorAt(0.55, theme.qcolor(self._line.name(), 0.07 * e))
        glow.setColorAt(1.0, theme.qcolor(self._line.name(), 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow))
        p.drawEllipse(QPointF(0, 0), radius * 1.55, radius * 1.55)
        top = theme.mix(self._line, QColor("#e6fdff"), 0.25)
        bottom = theme.mix(QColor(theme.VIOLET), QColor("#e08cff"), 0.6)
        if self._state == "ERROR":
            bottom = theme.mix(bottom, QColor(theme.RED), 0.5)
        self._blob.paint(p, radius, top, bottom, 0.55 + 0.45 * e)

    # -- mouse: click to talk, drag to spin, hover to ripple, right-click for sensitivity -------------
    def _on_blob(self, pos: QPointF) -> bool:
        d = pos - self._blob_center
        return self._blob_on and self._blob_radius > 0 and math.hypot(d.x(), d.y()) <= self._blob_radius

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._on_blob(event.position()):
            self._press, self._dragged = event.position(), False
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        if self._press is not None:
            delta = pos - self._press
            if self._dragged or math.hypot(delta.x(), delta.y()) > 4:
                self._dragged = True
                self._blob.drag(delta.x(), delta.y())
                self._press = pos
                self.update()
            return
        on = self._on_blob(pos)
        self.setCursor(Qt.CursorShape.PointingHandCursor if on else Qt.CursorShape.ArrowCursor)
        if on:
            r = max(self._blob_radius / 1.25, 1.0)
            self._blob.hover((pos.x() - self._blob_center.x()) / r, (pos.y() - self._blob_center.y()) / r)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._press is not None and event.button() == Qt.MouseButton.LeftButton:
            clicked = not self._dragged and self._on_blob(event.position())
            self._press, self._dragged = None, False
            if clicked:
                self._pulses.append([0.0, 1.0])
                self.talk_requested.emit()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.addSection("3D voice blob")
        show = menu.addAction("Show the 3D blob")
        show.setCheckable(True)
        show.setChecked(self._blob_on)
        menu.addSeparator()
        presets = {}
        for label, value in BLOB_SENSITIVITY_PRESETS:
            action = menu.addAction(f"Sensitivity: {label}")
            action.setCheckable(True)
            action.setChecked(abs(self._blob_sensitivity - value) < 0.05)
            presets[action] = value
        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return
        enabled, sensitivity = self._blob_on, self._blob_sensitivity
        if chosen is show:
            enabled = show.isChecked()
        elif chosen in presets:
            sensitivity = presets[chosen]
        self.set_blob(enabled, sensitivity)
        self.blob_settings_changed.emit(enabled, sensitivity)

    def _paint_orb(self, p: QPainter, R: float, e: float) -> None:
        breathe = 0.5 + 0.5 * math.sin(self._phase * 2.2)
        radius = R * (0.34 + 0.02 * breathe + 0.06 * self._level)
        energy = self._energy_color
        grad = QRadialGradient(QPointF(0, 0), radius)
        grad.setColorAt(0.0, theme.qcolor("#fff3e0", min(1.0, 0.25 + 0.75 * e)))
        grad.setColorAt(0.18, theme.qcolor(energy.name(), 0.85 * e + 0.1))
        grad.setColorAt(0.55, theme.qcolor(energy.name(), 0.28 * e))
        grad.setColorAt(1.0, theme.qcolor(energy.name(), 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawEllipse(QPointF(0, 0), radius, radius)
        # dense textured core ring (orange "machinery" in the reference)
        inner = R * 0.26
        # Four counter-rotating rings of dots (cheaper than dashed circles, same look).
        dots = QPolygonF()
        for k in range(4):
            r = inner * (0.55 + 0.15 * k)
            rot = self._phase * (0.6 if k % 2 else -0.8)
            count = 28 + 8 * k
            for i in range(count):
                a = rot + TAU * i / count
                dots.append(QPointF(math.cos(a) * r, math.sin(a) * r))
        p.setPen(QPen(theme.qcolor(energy.name(), 0.25 + 0.55 * e), 1.2, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.SquareCap))
        p.drawPoints(dots)

    def _paint_blades(self, p: QPainter, R: float, line: QColor, e: float) -> None:
        p.save()
        p.rotate(math.degrees(self._phase * 0.35))
        blade = QPainterPath()
        blade.moveTo(R * 0.07, 0)
        blade.lineTo(R * 0.17, -R * 0.022)
        blade.lineTo(R * 0.30, 0)
        blade.lineTo(R * 0.17, R * 0.022)
        blade.closeSubpath()
        fill = theme.qcolor(theme.PALE, 0.35 + 0.55 * e)
        p.setPen(QPen(theme.qcolor(line.name(), 0.6 + 0.4 * e), 1))
        p.setBrush(fill)
        for i in range(8):
            p.save()
            p.rotate(i * 45)
            scale = 1.0 + (0.18 * self._level if i % 2 == 0 else 0.0)
            p.scale(scale, 1.0)
            p.drawPath(blade)
            p.restore()
        p.restore()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.qcolor("#ffffff", 0.6 + 0.4 * e))
        p.drawEllipse(QPointF(0, 0), R * 0.035, R * 0.035)

    def _paint_sweep(self, p: QPainter, R: float, line: QColor) -> None:
        grad = QConicalGradient(QPointF(0, 0), -math.degrees(self._phase * 2.2))
        grad.setColorAt(0.0, theme.qcolor(line.name(), 0.45 * self._sweep))
        grad.setColorAt(0.12, theme.qcolor(line.name(), 0.0))
        grad.setColorAt(1.0, theme.qcolor(line.name(), 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawEllipse(QPointF(0, 0), R * 0.98, R * 0.98)

    def _paint_tags(self, p: QPainter, cx: float, cy: float, R: float, line: QColor, e: float) -> None:
        if not self._tags or self.width() < 560:
            return
        font = QFont("Consolas", 8)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        p.setFont(font)
        tag_w, tag_h = 170, 20
        x_tag = min(self.width() - tag_w - 8, cx + R * 1.18)
        for i, (key, value) in enumerate(self._tags):
            angle = -0.75 + i * 0.36
            anchor = QPointF(cx + math.cos(angle) * R * 0.92, cy + math.sin(angle) * R * 0.92)
            y = cy - R * 0.72 + i * (tag_h + 14)
            elbow = QPointF(x_tag - 22, y + tag_h / 2)
            p.setPen(QPen(theme.qcolor(theme.PALE, 0.35 + 0.3 * e), 1))
            p.drawLine(anchor, QPointF(elbow.x(), anchor.y()))
            p.drawLine(QPointF(elbow.x(), anchor.y()), elbow)
            p.drawLine(elbow, QPointF(x_tag, elbow.y()))
            p.setBrush(theme.qcolor(theme.PALE, 0.8))
            p.drawEllipse(anchor, 2.2, 2.2)
            rect = QRectF(x_tag, y, tag_w, tag_h)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.qcolor(theme.CYAN if i == 0 else theme.CYAN_DIM, 0.92))
            p.drawRect(rect)
            p.setPen(theme.qcolor(theme.BG))
            p.drawText(rect.adjusted(6, 0, -6, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, key)
            p.drawText(rect.adjusted(6, 0, -6, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, value[:18])

    def _paint_labels(self, p: QPainter, w: int, h: int, cy: float, R: float) -> None:
        y = min(h - 52, cy + R * 1.12 + 14)
        font = QFont("Consolas", 15)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 6)
        p.setFont(font)
        p.setPen(theme.qcolor(self._line.name(), 1.0))
        p.drawText(QRectF(0, y, w, 26), Qt.AlignmentFlag.AlignHCenter, self._state.replace("_", " "))
        font = QFont("Consolas", 9)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
        p.setFont(font)
        p.setPen(theme.qcolor(theme.MUTED))
        caption = self._detail if self._state == "ERROR" and self._detail else self._caption
        caption = caption if len(caption) <= 80 else caption[:77] + "…"
        p.drawText(QRectF(10, y + 28, w - 20, 18), Qt.AlignmentFlag.AlignHCenter, caption)
        # level meter under the label
        bar_w = 160
        x0 = (w - bar_w) / 2
        grad = QLinearGradient(x0, 0, x0 + bar_w, 0)
        grad.setColorAt(0, theme.qcolor(theme.CYAN_DIM, 0.9))
        grad.setColorAt(1, theme.qcolor(theme.ORANGE, 0.9))
        p.fillRect(QRectF(x0, y + 50, bar_w, 2), theme.qcolor(theme.LINE))
        p.fillRect(QRectF(x0, y + 50, bar_w * self._level, 2), QBrush(grad))

    # -- visibility: stop animating while hidden ------------------------------------------
    def showEvent(self, event) -> None:
        self._last_ms = 0
        self._timer.start(int(1000 / self._fps()))
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)

    @property
    def animating(self) -> bool:
        return self._timer.isActive()
