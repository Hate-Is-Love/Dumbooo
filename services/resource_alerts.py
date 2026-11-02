"""Background resource alerts (CPU, RAM, disk space, battery).

Samples ``SystemMonitor`` on its own thread every ``interval_s`` seconds (never
on the UI thread). A CPU/RAM alert fires only after the value has stayed above
its threshold for ``sustain_s`` seconds, so short spikes are ignored. Each
metric alerts at most once every ``COOLDOWN_S``. Thresholds come from
``config/automation.json`` → ``monitor`` and can be changed on the dashboard.
It only *reports*; it never closes programs or changes settings.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from app.event_bus import EventBus, Events

log = logging.getLogger(__name__)

COOLDOWN_S = 600


class ResourceAlerts:
    name = "resource_alerts"

    def __init__(self, bus: EventBus, monitor, settings: Callable[[], dict], clock=time.monotonic) -> None:
        self._bus = bus
        self._monitor = monitor
        self._settings = settings
        self._clock = clock
        self._above: dict[str, float] = {}
        self._last_alert: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.latest: dict = {}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ResourceAlerts", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(max(2.0, float(self._settings().get("interval_s", 10)))):
            try:
                self.check()
            except Exception:
                log.exception("Resource check failed")

    def check(self) -> list[dict]:
        """One measurement; returns (and publishes) any alerts that are due."""
        settings = self._settings()
        if not settings.get("alerts_enabled", True):
            return []
        snap = self._monitor.sample()
        now = self._clock()
        self.latest = {"cpu": snap.cpu_percent, "ram": snap.ram_percent, "disk_free_percent":
                       None if snap.disk_percent is None else 100 - snap.disk_percent,
                       "battery": snap.battery_percent, "charging": snap.battery_charging}
        alerts: list[dict] = []
        sustain = float(settings.get("sustain_s", 60))
        checks = [
            ("cpu", snap.cpu_percent, float(settings.get("cpu_alert_percent", 90)), True,
             lambda v, t: f"CPU load has been above {t:.0f}% (now {v:.0f}%) for over {sustain:.0f} seconds."),
            ("ram", snap.ram_percent, float(settings.get("ram_alert_percent", 90)), True,
             lambda v, t: f"Memory use has been above {t:.0f}% (now {v:.0f}%) for over {sustain:.0f} seconds."),
        ]
        for metric, value, threshold, sustained, text in checks:
            if value is None or value < threshold:
                self._above.pop(metric, None)
                continue
            since = self._above.setdefault(metric, now)
            if sustained and now - since < sustain:
                continue
            alerts += self._alert(metric, value, threshold, text(value, threshold), now)
        if snap.disk_percent is not None:
            free = 100 - snap.disk_percent
            limit = float(settings.get("disk_alert_free_percent", 10))
            if free < limit:
                alerts += self._alert("disk", free, limit, f"Only {snap.disk_free_gb:.1f} GB ({free:.0f}%) is free on "
                                                          f"{snap.disk_mount}.", now)
        if snap.battery_percent is not None and snap.battery_charging is False:
            limit = float(settings.get("battery_alert_percent", 15))
            if snap.battery_percent <= limit:
                alerts += self._alert("battery", snap.battery_percent, limit,
                                      f"Battery is at {snap.battery_percent:.0f}% and not charging.", now)
        return alerts

    def _alert(self, metric: str, value: float, threshold: float, message: str, now: float) -> list[dict]:
        last = self._last_alert.get(metric)
        if last is not None and now - last < COOLDOWN_S:
            return []
        self._last_alert[metric] = now
        alert = {"metric": metric, "value": round(value, 1), "threshold": threshold, "message": message}
        log.info("Resource alert: %s", message)
        self._bus.publish(Events.SYSTEM_ALERT, alert)
        return [alert]
