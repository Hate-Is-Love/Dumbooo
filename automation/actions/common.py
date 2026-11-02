"""Helpers shared by the capability modules."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable

from automation.config import AppEntry
from automation.platform.desktop import WindowInfo
from automation.registry import ArgumentError, ExecContext

_FILLER = re.compile(r"\b(?:the|app|application|program|window|please|my|a|an)\b", re.I)


def norm(text: str) -> str:
    return " ".join(_FILLER.sub(" ", (text or "").lower()).replace("-", " ").split())


def resolve_app(ctx: ExecContext, name: str) -> AppEntry | None:
    """An approved application by key, name or alias ("vs code", "chrome" …)."""
    apps = ctx.services.config.applications
    wanted = (name or "").strip().lower()
    if wanted in apps:
        return apps[wanted]
    target = norm(wanted)
    for app in apps.values():
        if wanted in app.names() or target in {norm(n) for n in app.names()}:
            return app
    return None


def require_app(ctx: ExecContext, name: str) -> AppEntry:
    app = resolve_app(ctx, name)
    if app is None:
        approved = ", ".join(sorted(a.name for a in ctx.services.config.applications.values()))
        raise LookupError(f"'{name}' is not an approved application. Approved: {approved}. "
                          "The owner can approve more on AUTOMATION → APPS & FOLDERS.")
    return app


def app_windows(ctx: ExecContext, app: AppEntry) -> list[WindowInfo]:
    process = app.process.lower()
    return [w for w in ctx.services.desktop.windows() if w.process.lower() == process]


def windows_matching(ctx: ExecContext, name: str) -> list[WindowInfo]:
    """Open windows for an approved app, or whose title contains ``name`` (for focusing only)."""
    app = resolve_app(ctx, name)
    if app is not None:
        found = app_windows(ctx, app)
        if found:
            return found
    words = norm(name)
    if not words:
        return []
    return [w for w in ctx.services.desktop.windows() if words in norm(w.title) or words == norm(Path(w.process).stem)]


def target_window(ctx: ExecContext, app: str | None) -> WindowInfo:
    """The window an input action is aimed at: a named app's window, else the last window the user used."""
    desktop = ctx.services.desktop
    if app:
        found = windows_matching(ctx, app)
        if not found:
            raise LookupError(f"I can't see an open window for '{app}'.")
        fg = desktop.foreground()
        for w in found:
            if fg is not None and w.hwnd == fg.hwnd:
                return w
        return found[0]
    last = desktop.last_external()
    if last is None:
        raise LookupError("I don't know which program you mean. Please name it, e.g. 'in Notepad'.")
    return last


def wait_until(ctx: ExecContext, predicate: Callable[[], object], timeout_s: float, interval: float = 0.25):
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            value = predicate()
        except Exception:
            value = None
        if value:
            return value
        if time.monotonic() >= deadline or ctx.cancelled:
            return value
        ctx.cancel.wait(interval)


def app_kind_of_window(ctx: ExecContext, window: WindowInfo) -> str:
    for app in ctx.services.config.applications.values():
        if app.process.lower() == window.process.lower():
            return app.kind
    return "other"


def check_path(ctx: ExecContext, raw: str, *, write: bool = False, must_exist: bool = False,
               container: bool = False) -> Path:
    if not raw:
        raise ArgumentError("a path is required")
    return ctx.services.guard.check(raw, write=write, must_exist=must_exist, container=container)


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
