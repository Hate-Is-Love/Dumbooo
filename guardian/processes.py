"""On-demand process and network inspection (psutil, read-only).

High CPU or memory use is reported as a fact, never as a sign of malware. Processes are never
terminated here (ending a process is Automation's "end process …", with its own confirmation).
"""

from __future__ import annotations

import ipaddress
import os
import time
from datetime import datetime

from guardian.files import path_observations, redact
from guardian.models import Indicator

UNUSUAL_PORTS = {4444: "often used by hacking tools (Metasploit default)", 1337: "often used by backdoors",
                 31337: "often used by backdoors", 6667: "IRC (old botnets used it)", 9050: "Tor proxy",
                 9001: "Tor relay", 5555: "Android debug bridge"}


def _psutil():
    import psutil

    return psutil


def list_processes(limit: int = 40, sort: str = "memory") -> list[dict]:
    psutil = _psutil()
    procs = []
    for p in psutil.process_iter(["pid", "name", "exe", "username", "memory_info"]):
        info = p.info
        try:
            cpu = p.cpu_percent(None)
        except Exception:
            cpu = 0.0
        procs.append({"pid": info["pid"], "name": info.get("name") or "?", "exe": info.get("exe") or "",
                      "user": (info.get("username") or "").split("\\")[-1],
                      "memory_mb": round((info["memory_info"].rss if info.get("memory_info") else 0) / 1e6, 1),
                      "cpu": cpu})
    if sort == "cpu":  # cpu_percent needs two samples
        time.sleep(0.5)
        for row in procs:
            try:
                row["cpu"] = round(psutil.Process(row["pid"]).cpu_percent(None) / (psutil.cpu_count() or 1), 1)
            except Exception:
                row["cpu"] = 0.0
    procs.sort(key=lambda r: r["cpu" if sort == "cpu" else "memory_mb"], reverse=True)
    return procs[:limit]


def find_processes(name_or_pid: str) -> list:
    psutil = _psutil()
    text = name_or_pid.strip().lower()
    if text.isdigit():
        try:
            return [psutil.Process(int(text))]
        except psutil.Error:
            return []
    if not text.endswith(".exe"):
        text_exe = text + ".exe"
    else:
        text_exe = text
    return [p for p in psutil.process_iter(["name"]) if (p.info.get("name") or "").lower() in (text, text_exe)]


def process_details(proc) -> tuple[dict, list[Indicator], list[str]]:
    """(facts, indicators, limitations) for one psutil.Process."""
    psutil = _psutil()
    facts: dict = {"PID": proc.pid}
    limitations: list[str] = []
    indicators: list[Indicator] = []

    def get(label, fn, fmt=lambda v: v):
        try:
            value = fn()
            facts[label] = fmt(value)
            return value
        except psutil.AccessDenied:
            facts[label] = "access denied"
            limitations.append(f"{label}: access denied (needs administrator rights)")
        except psutil.NoSuchProcess:
            raise
        except Exception:
            facts[label] = "unavailable"
        return None

    name = get("Name", proc.name)
    exe = get("Executable", proc.exe)
    get("Command", proc.cmdline, lambda v: redact(" ".join(v)))
    get("User", proc.username)
    get("Started", proc.create_time, lambda v: datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M:%S"))
    get("Memory", proc.memory_info, lambda v: f"{v.rss / 1e6:.0f} MB")
    try:
        proc.cpu_percent(None)
        time.sleep(0.3)
        facts["CPU"] = f"{proc.cpu_percent(None) / (psutil.cpu_count() or 1):.1f} %"
    except Exception:
        facts["CPU"] = "unavailable"
    parent = None
    try:
        parent = proc.parent()
    except Exception:
        pass
    facts["Parent"] = f"{parent.name()} (PID {parent.pid})" if parent else "none / not accessible"
    try:
        conns = proc.net_connections(kind="inet") if hasattr(proc, "net_connections") else proc.connections("inet")
        facts["Network connections"] = len(conns)
        for c in conns:
            if c.raddr and c.raddr.port in UNUSUAL_PORTS:
                indicators.append(Indicator(f"Connected to remote port {c.raddr.port} ({UNUSUAL_PORTS[c.raddr.port]})", 2))
    except psutil.AccessDenied:
        facts["Network connections"] = "access denied"
    except Exception:
        facts["Network connections"] = "unavailable"
    if exe:
        indicators += [Indicator(t, w) for t, w in path_observations(exe)]
    elif name and name.lower() not in ("system", "registry", "system idle process", "memory compression"):
        limitations.append("The program file could not be read (protected or system process)")
    return facts, indicators, limitations


def network_overview(limit: int = 60) -> dict:
    """Interfaces, addresses and active connections with owning programs (no traffic is captured)."""
    psutil = _psutil()
    interfaces = []
    stats = psutil.net_if_stats()
    for name, addrs in psutil.net_if_addrs().items():
        st = stats.get(name)
        ips = [a.address for a in addrs if getattr(a.family, "name", str(a.family)) in ("AF_INET", "AF_INET6")
               and not a.address.startswith("fe80")]
        if st and st.isup and ips:
            interfaces.append({"name": name, "addresses": ips, "speed_mbps": st.speed})
    names: dict[int, str] = {}
    connections, listening, notes = [], [], []
    try:
        conns = psutil.net_connections(kind="inet")
    except psutil.AccessDenied:
        conns, notes = [], ["Connections could not be listed: access denied (needs administrator rights)."]
    for c in conns:
        if c.pid and c.pid not in names:
            try:
                names[c.pid] = psutil.Process(c.pid).name()
            except Exception:
                names[c.pid] = "?"
        if not c.pid and c.status in ("TIME_WAIT", "CLOSE_WAIT", "FIN_WAIT1", "FIN_WAIT2", "LAST_ACK", "CLOSING"):
            program = "(closed connection)"  # already ended: Windows keeps it briefly, no program owns it any more
        else:
            program = names.get(c.pid, "unknown (no permission)" if not c.pid else "?")
        if c.status == "LISTEN":
            exposed = c.laddr.ip in ("0.0.0.0", "::")
            listening.append({"program": program, "pid": c.pid, "port": c.laddr.port, "all_interfaces": exposed})
        elif c.raddr:
            remote = c.raddr.ip
            try:
                public = ipaddress.ip_address(remote).is_global
            except ValueError:
                public = False
            row = {"program": program, "pid": c.pid, "local_port": c.laddr.port, "remote": remote,
                   "remote_port": c.raddr.port, "state": c.status, "public": public}
            if c.raddr.port in UNUSUAL_PORTS:
                row["note"] = UNUSUAL_PORTS[c.raddr.port]
                notes.append(f"{program} is connected to port {c.raddr.port} ({UNUSUAL_PORTS[c.raddr.port]}). "
                             "That alone is not proof of anything; check what the program is.")
            connections.append(row)
    # flagged first, then live connections to other machines; traffic between programs on this PC goes last
    connections.sort(key=lambda r: (not r.get("note"), r["state"] != "ESTABLISHED", _loopback(r["remote"]),
                                    r["program"].startswith("("), r["program"].lower()))
    exposed = sorted({(l["program"], l["port"]) for l in listening if l["all_interfaces"]})
    return {"interfaces": interfaces, "connections": connections[:limit], "total_connections": len(connections),
            "loopback": sum(1 for r in connections if _loopback(r["remote"])),
            "listening": listening, "exposed": exposed, "notes": notes,
            "programs": len({r["program"] for r in connections})}


def _loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def exe_of(pid: int) -> str:
    try:
        return _psutil().Process(pid).exe()
    except Exception:
        return ""


__all__ = ["list_processes", "find_processes", "process_details", "network_overview", "exe_of", "os"]
