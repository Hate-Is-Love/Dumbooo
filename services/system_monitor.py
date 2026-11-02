"""Live system metrics.

Every metric is either a real measurement or explicitly ``None`` ("unavailable").
Values are never invented: a desktop without a battery reports the battery as
unavailable rather than 100%.
"""

from __future__ import annotations

import logging
import platform
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a hard requirement, but stay usable
    psutil = None


@dataclass
class SystemSnapshot:
    cpu_percent: float | None
    ram_percent: float | None
    ram_used_gb: float | None
    ram_total_gb: float | None
    battery_percent: float | None
    battery_charging: bool | None
    os_name: str
    uptime_s: float
    services: dict[str, dict] = field(default_factory=dict)
    disk_percent: float | None = None      # system drive (Module 4)
    disk_free_gb: float | None = None
    disk_total_gb: float | None = None
    disk_mount: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def os_name() -> str:
    system, release, version = platform.system(), platform.release(), platform.version()
    if system == "Windows":
        # Windows 11 still reports release "10"; builds 22000+ are Windows 11.
        try:
            build = int(version.split(".")[2])
        except (IndexError, ValueError):
            build = 0
        if release == "10" and build >= 22000:
            release = "11"
        return f"Windows {release} (build {build}, {platform.machine()})"
    return f"{system} {release} ({platform.machine()})"


def format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


class SystemMonitor:
    """Collects a ``SystemSnapshot``. Call ``sample()`` from a worker thread."""

    def __init__(self, service_status: Callable[[], dict[str, dict]] | None = None,
                 started_at: float | None = None, ps=psutil) -> None:
        self._ps = ps
        self._started_at = started_at if started_at is not None else time.monotonic()
        self._service_status = service_status or (lambda: {})
        self._os_name = os_name()
        if self._ps is not None:
            try:
                self._ps.cpu_percent(interval=None)  # prime the counter
            except Exception:
                pass

    def sample(self) -> SystemSnapshot:
        cpu = ram = used = total = batt = None
        charging = None
        if self._ps is not None:
            try:
                cpu = float(self._ps.cpu_percent(interval=None))
            except Exception:
                log.debug("CPU metric unavailable", exc_info=True)
            try:
                vm = self._ps.virtual_memory()
                ram, used, total = float(vm.percent), vm.used / 1024**3, vm.total / 1024**3
            except Exception:
                log.debug("RAM metric unavailable", exc_info=True)
            try:
                battery = self._ps.sensors_battery()
                if battery is not None:
                    batt = float(battery.percent)
                    charging = bool(battery.power_plugged) if battery.power_plugged is not None else None
            except Exception:
                log.debug("Battery metric unavailable", exc_info=True)
        try:
            services = self._service_status()
        except Exception:
            log.exception("Service status unavailable")
            services = {}
        disk = self.system_disk()
        return SystemSnapshot(
            disk_percent=disk["percent"] if disk else None,
            disk_free_gb=disk["free_gb"] if disk else None,
            disk_total_gb=disk["total_gb"] if disk else None,
            disk_mount=disk["mount"] if disk else "",
            cpu_percent=cpu,
            ram_percent=ram,
            ram_used_gb=round(used, 2) if used is not None else None,
            ram_total_gb=round(total, 2) if total is not None else None,
            battery_percent=batt,
            battery_charging=charging,
            os_name=self._os_name,
            uptime_s=time.monotonic() - self._started_at,
            services=services,
        )

    # -- Module 4: detailed measurements (call from worker threads) ----------------------------
    def disks(self) -> list[dict]:
        """Capacity of every local drive (removable/optical drives without media are skipped)."""
        out: list[dict] = []
        if self._ps is None:
            return out
        try:
            partitions = self._ps.disk_partitions(all=False)
        except Exception:
            log.debug("Disk partitions unavailable", exc_info=True)
            return out
        for part in partitions:
            if "cdrom" in (part.opts or "") or not part.fstype:
                continue
            try:
                usage = self._ps.disk_usage(part.mountpoint)
            except Exception:
                continue
            out.append({"mount": part.mountpoint, "fstype": part.fstype, "total_gb": round(usage.total / 1024**3, 1),
                        "used_gb": round(usage.used / 1024**3, 1), "free_gb": round(usage.free / 1024**3, 1),
                        "percent": float(usage.percent)})
        return out

    def system_disk(self) -> dict | None:
        import os as _os

        drive = (_os.environ.get("SystemDrive", "C:") + "\\") if platform.system() == "Windows" else "/"
        if self._ps is None:
            return None
        try:
            usage = self._ps.disk_usage(drive)
        except Exception:
            return None
        return {"mount": drive, "total_gb": round(usage.total / 1024**3, 1), "free_gb": round(usage.free / 1024**3, 1),
                "used_gb": round(usage.used / 1024**3, 1), "percent": float(usage.percent)}

    def top_processes(self, by: str = "memory", limit: int = 8, interval: float = 0.6) -> list[dict]:
        """Processes using the most memory or CPU. CPU is measured over ``interval`` seconds
        and shown as a share of the whole processor (all cores = 100%)."""
        if self._ps is None:
            return []
        cores = self._ps.cpu_count() or 1
        procs = []
        for p in self._ps.process_iter(["pid", "name", "memory_info"]):
            try:
                if by == "cpu":
                    p.cpu_percent(None)
                procs.append(p)
            except Exception:
                continue
        if by == "cpu":
            time.sleep(interval)
        rows = []
        total = None
        try:
            total = self._ps.virtual_memory().total
        except Exception:
            pass
        for p in procs:
            try:
                mem = p.info.get("memory_info")
                rss = mem.rss if mem else 0
                cpu = p.cpu_percent(None) / cores if by == "cpu" else None
                name = p.info.get("name") or "?"
                if p.info.get("pid") in (0, 4) or name in ("System Idle Process", "Idle"):
                    continue
                rows.append({"pid": p.info["pid"], "name": name, "memory_mb": round(rss / 1024**2, 1),
                             "memory_percent": round(100 * rss / total, 1) if total else None,
                             "cpu_percent": round(cpu, 1) if cpu is not None else None})
            except Exception:
                continue
        if by == "cpu":
            rows.sort(key=lambda r: r["cpu_percent"] or 0, reverse=True)
        else:
            # group by program name: a browser is many processes
            grouped: dict[str, dict] = {}
            for r in rows:
                g = grouped.setdefault(r["name"], {"name": r["name"], "memory_mb": 0.0, "memory_percent": 0.0,
                                                   "processes": 0, "pid": r["pid"], "cpu_percent": None})
                g["memory_mb"] = round(g["memory_mb"] + r["memory_mb"], 1)
                g["memory_percent"] = round((g["memory_percent"] or 0) + (r["memory_percent"] or 0), 1)
                g["processes"] += 1
            rows = sorted(grouped.values(), key=lambda r: r["memory_mb"], reverse=True)
        return rows[:limit]

    def network(self, check_internet: bool = True) -> dict:
        import socket

        up: list[str] = []
        if self._ps is not None:
            try:
                for name, stats in self._ps.net_if_stats().items():
                    if stats.isup and "loopback" not in name.lower():
                        up.append(name)
            except Exception:
                log.debug("Network interfaces unavailable", exc_info=True)
        result: dict = {"interfaces_up": up, "internet": None, "latency_ms": None}
        if check_internet:
            start = time.monotonic()
            for host in (("1.1.1.1", 443), ("8.8.8.8", 443)):
                try:
                    with socket.create_connection(host, timeout=2.5):
                        result["internet"] = True
                        result["latency_ms"] = round((time.monotonic() - start) * 1000)
                        break
                except OSError:
                    result["internet"] = False
        return result

    def os_info(self) -> dict:
        info = {"os": self._os_name, "machine": platform.machine(), "processor": platform.processor(),
                "python": platform.python_version()}
        if self._ps is not None:
            try:
                info["cpu_cores_logical"] = self._ps.cpu_count()
                info["cpu_cores_physical"] = self._ps.cpu_count(logical=False)
                info["ram_total_gb"] = round(self._ps.virtual_memory().total / 1024**3, 1)
                info["boot_time"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(self._ps.boot_time()))
            except Exception:
                pass
        return info
