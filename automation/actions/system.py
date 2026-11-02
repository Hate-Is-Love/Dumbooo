"""Feature 5: real-time system monitoring.

All numbers are measured at the moment of the request with psutil (nothing
is estimated by the AI). Saved notes about the laptop (e.g. "my laptop has
8 GB RAM") are shown separately and labelled as saved, not measured.
Processes are never ended automatically; ``system.end_process`` is a
high-risk step that always asks first and refuses Windows system processes.
"""

from __future__ import annotations

import os

from automation.actions.apps import CRITICAL_PROCESSES
from automation.models import ActionResult, Risk
from automation.registry import Capability, CapabilityRegistry, ExecContext, Param
from automation.safety import SafetyError

SYSTEM_NAMES = {"memcompression", "svchost.exe", "system", "registry", "csrss.exe", "smss.exe", "wininit.exe",
                "services.exe", "lsass.exe", "dwm.exe", "msmpeng.exe", "searchindexer.exe", "wmiprvse.exe",
                "fontdrvhost.exe", "winlogon.exe", "spoolsv.exe", "audiodg.exe", "secure system", "vmmem",
                "vmmemwsl", "searchhost.exe", "runtimebroker.exe", "sihost.exe", "ctfmon.exe", "taskhostw.exe"}


def _saved_notes(ctx: ExecContext) -> str:
    if not ctx.user.can("memory.use"):
        return ""
    try:
        rows = ctx.services.memory.search(ctx.session, "laptop computer ram memory cpu processor hardware disk ssd "
                                                       "battery performance slow", limit=4)
    except Exception:
        return ""
    if not rows:
        return ""
    lines = [f"- {r['memory_key']}: {r['memory_value']} (saved {r['updated_at'][:10]})" for r in rows]
    return "\n\nFrom your saved notes (not measured now):\n" + "\n".join(lines)


def _gb(v: float | None) -> str:
    return "unavailable" if v is None else f"{v:.1f} GB"


def _status(ctx: ExecContext, args: dict) -> ActionResult:
    monitor = ctx.services.monitor
    focus = args.get("focus") or "overview"
    snap = monitor.sample()
    lines: list[str] = []
    data: dict = {"cpu_percent": snap.cpu_percent, "ram_percent": snap.ram_percent}
    if focus in ("overview", "cpu"):
        lines.append(f"CPU load: {'unavailable' if snap.cpu_percent is None else f'{snap.cpu_percent:.0f}%'}")
    if focus in ("overview", "memory"):
        if snap.ram_percent is None:
            lines.append("Memory: unavailable")
        else:
            free = snap.ram_total_gb - snap.ram_used_gb
            lines.append(f"Memory: {snap.ram_percent:.0f}% used — {snap.ram_used_gb:.1f} of {snap.ram_total_gb:.1f} GB "
                         f"in use, about {free:.1f} GB available")
            data["ram_available_gb"] = round(free, 1)
    if focus in ("overview", "disk"):
        disks = monitor.disks()
        data["disks"] = disks
        for d in disks:
            lines.append(f"Disk {d['mount']} {d['free_gb']:.1f} GB free of {d['total_gb']:.1f} GB ({d['percent']:.0f}% "
                         "used)")
        if not disks:
            lines.append("Disk: unavailable")
    if focus in ("overview", "battery"):
        if snap.battery_percent is None:
            lines.append("Battery: none detected (or unavailable)")
        else:
            state = {True: "charging", False: "on battery", None: "charging state unknown"}[snap.battery_charging]
            lines.append(f"Battery: {snap.battery_percent:.0f}% ({state})")
        data["battery_percent"] = snap.battery_percent
    if focus in ("overview", "network"):
        net = monitor.network()
        data["network"] = net
        if net["internet"]:
            lines.append(f"Network: connected to the internet ({net['latency_ms']} ms) via "
                         f"{', '.join(net['interfaces_up'][:3]) or 'an active adapter'}")
        elif net["interfaces_up"]:
            lines.append(f"Network: adapter up ({', '.join(net['interfaces_up'][:3])}) but no internet connection")
        else:
            lines.append("Network: no active network adapter")
    if focus in ("overview", "os"):
        info = monitor.os_info()
        data["os"] = info
        lines.append(f"System: {info['os']}, {info.get('cpu_cores_physical', '?')} cores / "
                     f"{info.get('cpu_cores_logical', '?')} threads, {info.get('ram_total_gb', '?')} GB RAM, "
                     f"running since {info.get('boot_time', '?')}")
    if focus == "processes":
        return _processes(ctx, {"by": "memory", "limit": 8})
    return ActionResult.done("Measured just now:\n" + "\n".join(f"- {l}" for l in lines),
                             "live measurements from the operating system", **data)


def _rows_text(rows: list[dict], by: str) -> str:
    out = []
    for r in rows:
        if by == "cpu":
            out.append(f"- {r['name']}: {r['cpu_percent']:.1f}% CPU, {r['memory_mb']:.0f} MB")
        else:
            procs = f" ({r['processes']} processes)" if r.get("processes", 1) > 1 else ""
            out.append(f"- {r['name']}{procs}: {r['memory_mb']:.0f} MB ({r['memory_percent'] or 0:.1f}% of RAM)")
    return "\n".join(out)


def _processes(ctx: ExecContext, args: dict) -> ActionResult:
    by = args.get("by") or "memory"
    limit = int(args.get("limit") or 8)
    rows = ctx.services.monitor.top_processes(by, limit + 6)
    apps = [r for r in rows if r["name"].lower() not in SYSTEM_NAMES][:limit]
    system = [r for r in rows if r["name"].lower() in SYSTEM_NAMES][:3]
    text = f"Top programs by {'CPU' if by == 'cpu' else 'memory'} right now:\n" + _rows_text(apps, by)
    if system:
        text += "\n\nWindows itself: " + ", ".join(f"{r['name']} {r['memory_mb']:.0f} MB" for r in system)
    return ActionResult.done(text, "measured from running processes just now", processes=apps)


def _diagnose(ctx: ExecContext, args: dict) -> ActionResult:
    monitor = ctx.services.monitor
    snap = monitor.sample()
    cpu_rows = monitor.top_processes("cpu", 5)
    mem_rows = [r for r in monitor.top_processes("memory", 8) if r["name"].lower() not in SYSTEM_NAMES][:4]
    disk = monitor.system_disk()
    findings, tips = [], []
    if snap.cpu_percent is not None and snap.cpu_percent >= 80:
        top = ", ".join(f"{r['name']} ({r['cpu_percent']:.0f}%)" for r in cpu_rows[:3])
        findings.append(f"The processor is busy: {snap.cpu_percent:.0f}% load. Biggest users: {top}.")
        tips.append("Close or pause the programs using the most CPU (or wait for updates/scans to finish).")
    if snap.ram_percent is not None and snap.ram_percent >= 85:
        top = ", ".join(f"{r['name']} ({r['memory_mb']:.0f} MB)" for r in mem_rows[:3])
        findings.append(f"Memory is nearly full: {snap.ram_percent:.0f}% used. Biggest users: {top}.")
        tips.append("Close browser tabs or programs you don't need; memory pressure makes Windows use the slower disk.")
    if disk and disk["percent"] >= 90:
        findings.append(f"The system drive is almost full: only {disk['free_gb']:.1f} GB free.")
        tips.append("Free up space (Downloads, Recycle Bin, Storage Sense). Below ~10% free, Windows slows down.")
    if snap.battery_percent is not None and snap.battery_charging is False and snap.battery_percent < 20:
        findings.append(f"Battery is low ({snap.battery_percent:.0f}%) — Windows may be limiting performance to save "
                        "power.")
        tips.append("Plug in the charger.")
    if not findings:
        cpu = "unavailable" if snap.cpu_percent is None else f"{snap.cpu_percent:.0f}%"
        ram = "unavailable" if snap.ram_percent is None else f"{snap.ram_percent:.0f}%"
        findings.append(f"Nothing is overloaded right now (CPU {cpu}, memory {ram}"
                        + (f", {disk['free_gb']:.0f} GB free on {disk['mount']}" if disk else "") + ").")
        tips.append("If it felt slow earlier, it may have been a temporary spike; ask me again while it's slow.")
        if mem_rows:
            tips.append("Largest memory users now: " + ", ".join(f"{r['name']} ({r['memory_mb']:.0f} MB)"
                                                                 for r in mem_rows[:3]) + ".")
    text = ("What I measured just now:\n" + "\n".join(f"- {f}" for f in findings) + "\n\nWhat you can do:\n" +
            "\n".join(f"- {t}" for t in tips) + _saved_notes(ctx) +
            "\n\nI haven't closed or changed anything.")
    return ActionResult.done(text, "live CPU, memory, disk and battery measurements", findings=findings)


def _end_precheck(ctx: ExecContext, args: dict) -> None:
    name = args["name"].lower()
    if not name.endswith(".exe") and "." not in name:
        args["name"] = name = name + ".exe"
    if name in CRITICAL_PROCESSES or name in SYSTEM_NAMES:
        raise SafetyError(f"{args['name']} is part of Windows and must not be ended.")
    own = os.path.basename(__import__("sys").executable).lower()
    if name in (own, "python.exe", "pythonw.exe"):
        raise SafetyError("That would also end DUMBOO itself. Please end it manually in Task Manager.")
    if not ctx.services.desktop.process_running(args["name"]):
        raise LookupError(f"No process called {args['name']} is running.")


def _end_process(ctx: ExecContext, args: dict) -> ActionResult:
    import psutil

    victims = [p for p in psutil.process_iter(["name"]) if (p.info.get("name") or "").lower() == args["name"].lower()]
    for p in victims:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(victims, timeout=5)
    left = ctx.services.desktop.process_running(args["name"])
    ok = not left
    return ActionResult(ok, f"Ended {args['name']} ({len(victims)} process(es))." if ok else
                        f"{args['name']} is still running (it may need administrator rights).", verified=ok,
                        verification="no process with that name is left" if ok else "process still running")


def register(reg: CapabilityRegistry) -> None:
    reg.add(Capability("system.status", "Measure CPU, memory, disk, battery, network or OS information now",
                       {"focus": Param(str, required=False, default="overview",
                                       choices=("overview", "cpu", "memory", "disk", "battery", "network", "os",
                                                "processes"))},
                       _status, "automation.basic", Risk.LOW, timeout_s=30,
                       describe=lambda a: f"Measure {a.get('focus') or 'system'} status"))
    reg.add(Capability("system.processes", "List the programs using the most memory or CPU right now",
                       {"by": Param(str, required=False, default="memory", choices=("memory", "cpu")),
                        "limit": Param(int, required=False, default=8, minimum=1, maximum=25)},
                       _processes, "automation.basic", Risk.LOW, timeout_s=30,
                       describe=lambda a: f"List top programs by {a.get('by') or 'memory'}"))
    reg.add(Capability("system.diagnose", "Explain why the laptop may be slow, from live measurements", {},
                       _diagnose, "automation.basic", Risk.LOW, timeout_s=30,
                       describe=lambda a: "Check why the laptop is slow"))
    reg.add(Capability("system.end_process", "End a running program by process name (asks first)",
                       {"name": Param(str, max_len=80)}, _end_process, "automation.basic", Risk.HIGH,
                       min_risk=Risk.HIGH, precheck=_end_precheck,
                       confirm_details=lambda c, a: f"{a['name']} will be ended. Unsaved work in it will be lost.",
                       describe=lambda a: f"End the program {a['name']}"))
