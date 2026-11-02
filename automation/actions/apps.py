"""Feature 1: intelligent application control (approved applications only)."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from automation.actions.common import (app_windows, check_path, norm, require_app, resolve_app, wait_until,
                                       windows_matching)
from automation.config import ConfigError
from automation.models import ActionResult, Risk
from automation.registry import ArgumentError, Capability, CapabilityRegistry, ExecContext, Param
from automation.safety import SafetyError, check_url, is_executable

log = logging.getLogger(__name__)

LAUNCH_WAIT_S = 15
CRITICAL_PROCESSES = {"system", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
                      "lsass.exe", "svchost.exe", "dwm.exe", "explorer.exe", "fontdrvhost.exe", "memcompression",
                      "securityhealthservice.exe", "msmpeng.exe", "sihost.exe", "ctfmon.exe", "audiodg.exe"}


def _target_kind(target: str) -> str:
    if re.match(r"^https?://", target, re.I) or re.match(r"^[\w.-]+\.[a-z]{2,}(/.*)?$", target, re.I) \
            and not os.path.isabs(target):
        return "url"
    return "folder" if Path(target).is_dir() else "file"


def _open_precheck(ctx: ExecContext, args: dict) -> None:
    app = require_app(ctx, args["app"])
    target = args.get("target") or ""
    if not target:
        return
    kind = _target_kind(target)
    if kind not in app.opens:
        raise ArgumentError(f"{app.name} can't open a {kind} (it opens: {', '.join(app.opens) or 'nothing'}).")
    if kind == "url":
        args["target"] = check_url(target, ctx.services.config.browser.get("blocked_domains"))
    else:
        path = check_path(ctx, target, must_exist=True)
        if kind == "file" and is_executable(path) and app.kind != "editor":
            # an editor shows a script's text; any other program might run it
            raise SafetyError("DUMBOO only opens programs and scripts in a code/text editor, never runs them.")
        args["target"] = str(path)


def _open(ctx: ExecContext, args: dict) -> ActionResult:
    app = require_app(ctx, args["app"])
    desktop = ctx.services.desktop
    target = args.get("target") or ""
    before = {w.hwnd: w.title for w in app_windows(ctx, app)}
    if app.uri:
        desktop.shell_open(app.uri)
    else:
        exe = app.resolve()
        if exe is None:
            return ActionResult.fail(f"{app.name} is approved but not installed in any configured location "
                                     f"({'; '.join(app.paths)}).")
        try:
            desktop.launch(exe, [target] if target else [])
        except OSError as exc:
            return ActionResult.fail(f"{app.name} could not be started: {exc.strerror or exc}.")
    ctx.progress(f"waiting for {app.name} to appear")
    name_hint = ""
    if target and _target_kind(target) != "url":
        name_hint = Path(target).name.lower()

    def appeared():
        windows = app_windows(ctx, app)
        if name_hint:
            if app.kind == "files":
                folders = [p for _h, p in desktop.explorer_folders()]
                if any(Path(p) == Path(target) for p in folders):
                    return [w for w in windows if Path(target).name.lower() in w.title.lower()] or windows[:1]
            matched = [w for w in windows if name_hint in w.title.lower()]
            return matched
        new = [w for w in windows if w.hwnd not in before]
        if new:
            return new
        if target:  # a URL opened as a new tab: some window of the app changed its title
            return [w for w in windows if before.get(w.hwnd) not in (None, w.title)]
        return None

    found = wait_until(ctx, appeared, LAUNCH_WAIT_S)
    if ctx.cancelled:
        return ActionResult.fail(f"Stopped while waiting for {app.name}.")
    if not found and not target and before:
        found = app_windows(ctx, app)  # it was already open; the existing window is reused
    if not found:
        running = desktop.process_running(app.process)
        if running:
            return ActionResult(True, f"Started {app.name}" + (f" with {target}" if target else ""), verified=False,
                                verification=f"{app.process} is running, but no matching window appeared within "
                                             f"{LAUNCH_WAIT_S} seconds")
        return ActionResult.fail(f"{app.name} did not start (no {app.process} process and no window appeared).")
    window = found[0]
    focused = desktop.focus(window.hwnd)
    what = f"{app.name}" + (f" with {Path(target).name if _target_kind(target) != 'url' else target}" if target else "")
    reused = not target and window.hwnd in before
    return ActionResult.done(
        f"{'Brought ' + app.name + ' to the front (it was already open)' if reused else 'Opened ' + what}.",
        f"window “{window.title[:80]}” ({window.process}) is open" + (" and in front" if focused else ""),
        app=app.key, hwnd=window.hwnd, title=window.title, path=target if target and _target_kind(target) != "url"
        else "")


def _focus(ctx: ExecContext, args: dict) -> ActionResult:
    windows = windows_matching(ctx, args["app"])
    if not windows:
        app = resolve_app(ctx, args["app"])
        if app is not None:
            return ActionResult.fail(f"{app.name} is not open. Say “open {app.name}” to start it.")
        return ActionResult.fail(f"I can't see an open window matching '{args['app']}'.")
    window = windows[0]
    if not ctx.services.desktop.focus(window.hwnd):
        return ActionResult.fail(f"Windows did not let me bring “{window.title[:60]}” to the front. "
                                 "Click it in the taskbar instead.")
    fg = ctx.services.desktop.foreground()
    ok = fg is not None and fg.hwnd == window.hwnd
    return ActionResult(True, f"Switched to “{window.title[:60]}”.", verified=ok,
                        verification="it is now the active window" if ok else "the active window did not change",
                        data={"hwnd": window.hwnd, "title": window.title})


def _window_state(ctx: ExecContext, args: dict) -> ActionResult:
    windows = windows_matching(ctx, args["app"])
    if not windows:
        return ActionResult.fail(f"I can't see an open window for '{args['app']}'.")
    action = args["action"]
    desktop = ctx.services.desktop
    targets = windows if args.get("all_windows") else windows[:1]
    for w in targets:
        if action == "restore" or action == "maximize":
            desktop.set_state(w.hwnd, action)
            if action == "restore":
                desktop.focus(w.hwnd)
        else:
            desktop.set_state(w.hwnd, "minimize")

    def check():
        states = [desktop.window(w.hwnd) for w in targets]
        if any(s is None for s in states):
            return False
        if action == "minimize":
            return all(s.minimized for s in states)
        if action == "maximize":
            return all(s.maximized for s in states)
        return all(not s.minimized for s in states)

    ok = bool(wait_until(ctx, check, 3))
    verb = {"minimize": "Minimised", "maximize": "Maximised", "restore": "Restored"}[action]
    names = ", ".join(f"“{w.title[:40]}”" for w in targets[:3])
    return ActionResult(True, f"{verb} {names}.", verified=ok,
                        verification=f"window state is now {action}d" if ok else "the window state did not change")


def _close(ctx: ExecContext, args: dict) -> ActionResult:
    windows = windows_matching(ctx, args["app"])
    if not windows:
        return ActionResult.done(f"'{args['app']}' is not open, so there was nothing to close.",
                                 "no matching window exists")
    desktop = ctx.services.desktop
    for w in windows:
        desktop.close(w.hwnd)
    remaining = wait_until(ctx, lambda: not [w for w in windows if desktop.window(w.hwnd) is not None], 6)
    left = [desktop.window(w.hwnd) for w in windows]
    left = [w for w in left if w is not None]
    if not left:
        return ActionResult.done(f"Closed {len(windows)} window(s) of {args['app']}.",
                                 "the windows no longer exist", closed=len(windows))
    return ActionResult(True, f"Asked {args['app']} to close, but {len(left)} window(s) are still open — it is "
                              "probably asking whether to save your changes. Please answer it yourself.",
                        verified=False, verification="windows still open (unsaved work is never discarded "
                                                     "without your decision)", data={"remaining": len(left)})


def _force_close(ctx: ExecContext, args: dict) -> ActionResult:
    import psutil

    app = require_app(ctx, args["app"])
    victims = [p for p in psutil.process_iter(["name"]) if (p.info.get("name") or "").lower() == app.process.lower()]
    if not victims:
        return ActionResult.done(f"{app.name} is not running.", "no process found")
    for p in victims:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(victims, timeout=5)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    still = ctx.services.desktop.process_running(app.process)
    return ActionResult(not still, f"Force-closed {app.name} ({len(victims)} process(es))." if not still
                        else f"{app.name} could not be ended (it may be running as administrator).",
                        verified=not still, verification="no process left" if not still else "process still running")


def _force_close_precheck(ctx: ExecContext, args: dict) -> None:
    if require_app(ctx, args["app"]).process.lower() in CRITICAL_PROCESSES:
        raise SafetyError("That is a Windows system process and cannot be ended.")


def _status(ctx: ExecContext, args: dict) -> ActionResult:
    app = resolve_app(ctx, args["app"])
    windows = windows_matching(ctx, args["app"])
    pids = ctx.services.desktop.process_running(app.process) if app else []
    name = app.name if app else args["app"]
    if not windows and not pids:
        return ActionResult.done(f"{name} is not running.", "checked running processes and windows", running=False)
    titles = "; ".join(f"“{w.title[:50]}”" + (" (minimised)" if w.minimized else "") for w in windows[:5])
    return ActionResult.done(f"{name} is running" + (f" with {len(windows)} window(s): {titles}." if windows
                                                     else " in the background (no open window)."),
                             "checked running processes and windows", running=True, windows=len(windows))


def _list_windows(ctx: ExecContext, args: dict) -> ActionResult:
    windows = ctx.services.desktop.windows()
    lines = [f"- {w.title[:70]} ({w.process}{', minimised' if w.minimized else ''})" for w in windows[:25]]
    return ActionResult.done(f"{len(windows)} open window(s):\n" + "\n".join(lines), "read from Windows",
                             windows=[{"title": w.title, "process": w.process} for w in windows[:25]])


def _start_menu_candidates(query: str) -> list[dict]:
    roots = [Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs",
             Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs"]
    q = norm(query)
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for lnk in root.rglob("*.lnk"):
            if q and q in norm(lnk.stem):
                found.append({"name": lnk.stem, "shortcut": str(lnk), "target": _shortcut_target(lnk)})
            if len(found) >= 10:
                break
    return found


def _shortcut_target(lnk: Path) -> str:
    try:
        import comtypes
        import comtypes.client

        comtypes.CoInitialize()
        try:
            shell = comtypes.client.CreateObject("WScript.Shell", dynamic=True)
            return str(shell.CreateShortcut(str(lnk)).TargetPath or "")
        finally:
            comtypes.CoUninitialize()
    except Exception:
        return ""


def _find_installed(ctx: ExecContext, args: dict) -> ActionResult:
    matches = [m for m in _start_menu_candidates(args["query"]) if m["target"].lower().endswith(".exe")]
    if not matches:
        return ActionResult.done(f"I couldn't find an installed program called '{args['query']}' in the Start menu.",
                                 "searched Start-menu shortcuts", candidates=[])
    lines = [f"- {m['name']}: {m['target']}" for m in matches]
    return ActionResult.done("Installed programs found (not approved yet):\n" + "\n".join(lines) +
                             "\n\nThe owner can approve one with “approve <name> as an application”.",
                             "searched Start-menu shortcuts", candidates=matches)


def _approve_precheck(ctx: ExecContext, args: dict) -> None:
    path = Path(args["path"])
    if not path.is_absolute() or not path.is_file():
        raise ArgumentError(f"{args['path']} is not an existing program file.")


def _approve(ctx: ExecContext, args: dict) -> ActionResult:
    key = re.sub(r"[^a-z0-9_-]+", "-", args["name"].lower()).strip("-")[:40] or "app"
    exe = Path(args["path"])
    try:
        entry = ctx.services.settings.add_application(key, {
            "name": args["name"], "aliases": [args["name"].lower()], "paths": [str(exe)], "process": exe.name,
            "kind": args.get("kind") or "other", "opens": ["file"]})
    except ConfigError as exc:
        return ActionResult.fail(str(exc))
    ok = ctx.services.config.applications.get(key) == entry
    return ActionResult(True, f"{entry.name} is now an approved application.", verified=ok,
                        verification="saved in the automation configuration", data={"key": key})


def register(reg: CapabilityRegistry) -> None:
    app_param = Param(str, max_len=80, help="approved application name, e.g. chrome, vscode, notepad")
    reg.add(Capability("apps.open", "Open an approved application, optionally with a file, folder or URL",
                       {"app": app_param, "target": Param(str, required=False, default="", max_len=2000,
                                                          help="file/folder path or URL to open with it")},
                       _open, "automation.basic", Risk.LOW, precheck=_open_precheck,
                       describe=lambda a: f"Open {a['app']}" + (f" with {a['target']}" if a.get("target") else ""),
                       timeout_s=40, example='{"app": "vscode", "target": "C:\\\\Users\\\\me\\\\Projects\\\\demo"}'))
    reg.add(Capability("apps.focus", "Bring an open application window to the front (switch to it)",
                       {"app": Param(str, max_len=120, help="app name or words from the window title")},
                       _focus, "automation.basic", Risk.LOW, describe=lambda a: f"Switch to {a['app']}"))
    reg.add(Capability("apps.window", "Minimise, maximise or restore an application window",
                       {"app": Param(str, max_len=120), "action": Param(str, choices=("minimize", "maximize", "restore")),
                        "all_windows": Param(bool, required=False, default=False)},
                       _window_state, "automation.basic", Risk.LOW,
                       describe=lambda a: f"{a['action'].capitalize()} {a['app']}"))
    reg.add(Capability("apps.close", "Close an application's windows normally (it may ask to save first)",
                       {"app": Param(str, max_len=120)}, _close, "automation.basic", Risk.LOW,
                       describe=lambda a: f"Close {a['app']}", timeout_s=30))
    reg.add(Capability("apps.force_close", "End an application's processes immediately (unsaved work is lost)",
                       {"app": app_param}, _force_close, "automation.basic", Risk.HIGH, min_risk=Risk.HIGH,
                       precheck=_force_close_precheck,
                       describe=lambda a: f"Force-close {a['app']} (unsaved work will be lost)",
                       confirm_details=lambda c, a: f"{require_app(c, a['app']).name} will be ended immediately. "
                                                    "Any unsaved work in it will be lost."))
    reg.add(Capability("apps.status", "Check whether an application is running", {"app": Param(str, max_len=120)},
                       _status, "automation.basic", Risk.LOW, describe=lambda a: f"Check whether {a['app']} is running"))
    reg.add(Capability("apps.list_windows", "List the open application windows", {}, _list_windows,
                       "automation.basic", Risk.LOW, describe=lambda a: "List open windows"))
    reg.add(Capability("apps.find_installed", "Look for an installed program in the Start menu (does not open it)",
                       {"query": Param(str, max_len=80)}, _find_installed, "automation.basic", Risk.LOW,
                       describe=lambda a: f"Look for installed program '{a['query']}'"))
    reg.add(Capability("apps.approve", "Approve a program so DUMBOO may open it (owner only)",
                       {"name": Param(str, max_len=60), "path": Param(str, max_len=500),
                        "kind": Param(str, required=False, default="other",
                                      choices=("browser", "editor", "files", "utility", "office", "media",
                                               "communication", "other"))},
                       _approve, "automation.admin", Risk.HIGH, min_risk=Risk.HIGH, precheck=_approve_precheck,
                       describe=lambda a: f"Approve {a['name']} ({a['path']}) as an application", planner=False,
                       confirm_details=lambda c, a: f"DUMBOO will be allowed to start:\n{a['path']}"))
