"""System-wide wake shortcut (Ctrl+Alt+D) on Windows.

Uses the Win32 ``RegisterHotKey`` API on a small message-loop thread, so the
shortcut works while the dashboard is hidden. On other platforms, or if
another program already owns the shortcut, ``start()`` reports that it is
unavailable and the tray icon remains the manual wake control.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Callable

log = logging.getLogger(__name__)

MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
HOTKEY_LABEL = "Ctrl+Alt+D"


class GlobalHotkey:
    def __init__(self, callback: Callable[[], None], key: str = "D") -> None:
        self._callback = callback
        self._vk = ord(key.upper())
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self.available = False
        self.error: str | None = None

    def start(self) -> bool:
        if sys.platform != "win32":
            self.error = "global shortcut only implemented on Windows"
            return False
        self._thread = threading.Thread(target=self._run, name="Hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(2)
        return self.available

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, self._vk):
            self.error = f"{HOTKEY_LABEL} is already used by another program"
            log.warning("Global hotkey unavailable: %s", self.error)
            self._ready.set()
            return
        self.available = True
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    try:
                        self._callback()
                    except Exception:
                        log.exception("Hotkey callback failed")
        finally:
            user32.UnregisterHotKey(None, 1)
            self.available = False

    def stop(self) -> None:
        if self._thread is None or self._thread_id is None:
            return
        import ctypes

        ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(timeout=1)
        self._thread = None
