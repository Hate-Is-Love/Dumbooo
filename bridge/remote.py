"""Laptop control from the phone (DUMBOO Mobile Phase 2): screen frames, mouse, keyboard and shortcuts.

Everything goes through DUMBOO's existing desktop layer (``automation/platform``). Only phones with the
``laptop_control`` capability (switched on per phone, with the password) whose user has ``automation.input`` reach
this module. Screen frames are captured only while the phone is showing the screen.
"""

from __future__ import annotations

import time

import logging
import threading
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

MAX_FRAME_WIDTH = 1600
MAX_TEXT = 500
MAX_SCROLL = 15
MAX_MOVE = 2000
BUTTONS = ("left", "right", "middle")


class RemoteError(ValueError):
    pass


def primary_monitor() -> tuple[int, int, int, int]:
    """(left, top, width, height) of the primary monitor, in desktop pixels."""
    import mss

    factory = getattr(mss, "MSS", None) or mss.mss
    with factory() as sct:
        m = sct.monitors[1]
        return m["left"], m["top"], m["width"], m["height"]


def grab_frame(max_width: int, quality: int) -> tuple[bytes, int, int]:
    """JPEG of the primary monitor, scaled to at most ``max_width`` pixels wide. Returns (jpeg, width, height)."""
    import cv2
    import mss

    factory = getattr(mss, "MSS", None) or mss.mss
    with factory() as sct:
        shot = sct.grab(sct.monitors[1])
    image = np.asarray(shot)[:, :, :3]                    # BGRA -> BGR
    height, width = image.shape[:2]
    if width > max_width:
        image = cv2.resize(image, (max_width, int(height * max_width / width)), interpolation=cv2.INTER_AREA)
    ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise RemoteError("the screen picture couldn't be encoded")
    return jpeg.tobytes(), width, height


def _check_keys(combo: str) -> None:
    """Only key names DUMBOO knows (checked here, not left to the desktop layer)."""
    try:
        from automation.platform.windows import VK
    except Exception:          # not on Windows: the desktop layer checks
        return
    unknown = [k for k in combo.replace(" ", "").split("+") if k and k not in VK]
    if unknown or not combo.replace("+", "").strip():
        raise RemoteError(f"unknown key '{unknown[0] if unknown else combo}'")


class RemoteControl:
    def __init__(self, desktop, monitor: Callable[[], tuple[int, int, int, int]] = primary_monitor,
                 frame: Callable[[int, int], tuple[bytes, int, int]] = grab_frame) -> None:
        self._desktop = desktop
        self._monitor_source = monitor
        self._monitor_cache: tuple[float, tuple[int, int, int, int]] | None = None
        self._frame = frame
        self._lock = threading.Lock()

    def _monitor(self) -> tuple[int, int, int, int]:
        """The primary monitor's position and size, re-read at most every 2 s (touchpad moves arrive ~100/s)."""
        now = time.monotonic()
        if self._monitor_cache is None or now - self._monitor_cache[0] > 2.0:
            self._monitor_cache = (now, self._monitor_source())
        return self._monitor_cache[1]

    def frame(self, max_width: int = 1024, quality: int = 60) -> tuple[bytes, int, int]:
        return self._frame(max(320, min(MAX_FRAME_WIDTH, int(max_width))), max(30, min(85, int(quality))))

    def _point(self, nx, ny) -> tuple[int, int]:
        """Normalised (0..1) screen position from the phone -> desktop pixels on the primary monitor."""
        if not isinstance(nx, (int, float)) or not isinstance(ny, (int, float)) or not (0 <= nx <= 1 and 0 <= ny <= 1):
            raise RemoteError("the position must be between 0 and 1")
        left, top, width, height = self._monitor()
        return left + int(nx * (width - 1)), top + int(ny * (height - 1))

    def click(self, nx=None, ny=None, button: str = "left", double: bool = False) -> None:
        if button not in BUTTONS:
            raise RemoteError("unknown mouse button")
        with self._lock:
            x, y = self._point(nx, ny) if nx is not None else self._desktop.cursor_position()
            self._desktop.click(x, y, button=button, double=bool(double))

    def move(self, dx, dy) -> None:
        """Touchpad: move the cursor by (dx, dy) pixels, kept on the primary monitor."""
        if not isinstance(dx, (int, float)) or not isinstance(dy, (int, float)):
            raise RemoteError("move needs numbers")
        dx, dy = max(-MAX_MOVE, min(MAX_MOVE, dx)), max(-MAX_MOVE, min(MAX_MOVE, dy))
        left, top, width, height = self._monitor()
        with self._lock:
            x, y = self._desktop.cursor_position()
            self._desktop.move_cursor(min(max(left, int(x + dx)), left + width - 1),
                                      min(max(top, int(y + dy)), top + height - 1))

    def scroll(self, clicks) -> None:
        if not isinstance(clicks, int) or clicks == 0:
            raise RemoteError("scroll needs a whole number of clicks")
        with self._lock:
            self._desktop.scroll(0, max(-MAX_SCROLL, min(MAX_SCROLL, clicks)))   # 0: wherever the cursor is

    def key(self, combo: str) -> None:
        if not isinstance(combo, str) or not combo.strip() or len(combo) > 40:
            raise RemoteError("key combination is required")
        combo = combo.strip().lower()
        _check_keys(combo)
        with self._lock:
            try:
                self._desktop.press(combo)
            except ValueError as exc:
                raise RemoteError(str(exc)) from exc

    def text(self, text: str) -> int:
        if not isinstance(text, str) or not text:
            raise RemoteError("text is required")
        if len(text) > MAX_TEXT:
            raise RemoteError(f"at most {MAX_TEXT} characters at a time")
        with self._lock:
            target = self._desktop.foreground()
            if target is None:
                raise RemoteError("no window is in front to type into")
            return self._desktop.type_text(text, target.hwnd, threading.Event())
