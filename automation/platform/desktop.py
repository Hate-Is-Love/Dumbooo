"""Desktop interface used by the automation capabilities."""

from __future__ import annotations

import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

# Environment variables that must never reach programs DUMBOO starts.
_SECRET_ENV = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")


def clean_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of DUMBOO's environment without API keys or other secrets (for launched programs)."""
    env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in _SECRET_ENV)}
    env.pop("DUMBOO_DATA_DIR", None)
    if extra:
        env.update(extra)
    return env


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    process: str           # executable name, e.g. "notepad.exe" (the real app for UWP frames)
    cls: str = ""
    minimized: bool = False
    maximized: bool = False
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)

    def as_dict(self) -> dict:
        return asdict(self)


class DesktopError(RuntimeError):
    pass


class Desktop(Protocol):
    available: bool

    def windows(self) -> list[WindowInfo]: ...
    def window(self, hwnd: int) -> WindowInfo | None: ...
    def foreground(self) -> WindowInfo | None: ...
    def last_external(self) -> WindowInfo | None: ...
    def focus(self, hwnd: int) -> bool: ...
    def set_state(self, hwnd: int, state: str) -> bool: ...
    def close(self, hwnd: int) -> bool: ...
    def type_text(self, text: str, guard_hwnd: int, cancel: threading.Event) -> int: ...
    def press(self, combo: str) -> None: ...
    def scroll(self, hwnd: int, clicks: int) -> None: ...
    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None: ...
    def get_clipboard(self) -> str | None: ...
    def set_clipboard(self, text: str) -> bool: ...
    def clipboard_sequence(self) -> int: ...
    def launch(self, exe: Path, args: list[str]) -> int: ...
    def shell_open(self, target: str) -> None: ...
    def process_running(self, name: str) -> list[int]: ...
    def explorer_folders(self) -> list[tuple[int, str]]: ...
    def recycle(self, paths: list[Path]) -> bool: ...
