"""Ordered start-up and shutdown of background services."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger(__name__)


class Service(Protocol):
    name: str

    def start(self) -> None: ...

    def stop(self) -> None: ...


@dataclass
class ServiceRecord:
    service: Service
    required: bool = False
    started: bool = False
    error: str | None = None


@dataclass
class Lifecycle:
    """Starts services in registration order and stops them in reverse order.

    A failing optional service (for example a missing microphone) is recorded
    and reported, but does not stop the rest of the application from running,
    so the text-only dashboard always remains usable.
    """

    records: list[ServiceRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stopped: bool = False

    def register(self, service: Service, required: bool = False) -> None:
        self.records.append(ServiceRecord(service, required))

    def start_all(self) -> list[ServiceRecord]:
        failures = []
        for record in self.records:
            try:
                record.service.start()
                record.started = True
                record.error = None
                log.info("Started service %s", record.service.name)
            except Exception as exc:
                record.error = str(exc)
                failures.append(record)
                log.exception("Service %s failed to start", record.service.name)
                if record.required:
                    raise
        return failures

    def stop_all(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        for record in reversed(self.records):
            if not record.started:
                continue
            try:
                record.service.stop()
                log.info("Stopped service %s", record.service.name)
            except Exception:
                log.exception("Service %s failed to stop cleanly", record.service.name)
            finally:
                record.started = False

    def status(self) -> dict[str, dict]:
        return {
            r.service.name: {"started": r.started, "error": r.error, "required": r.required}
            for r in self.records
        }
