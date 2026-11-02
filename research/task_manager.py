"""Research task bookkeeping: real progress stages, time/search/page budgets and cancellation.

Progress shown on the dashboard comes only from these stage changes (planning,
searching, reading sources, analysing, checking citations); nothing is animated
that isn't actually happening.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from app.event_bus import EventBus, Events

STAGES = {"search": ["Search the web", "Read sources", "Analyse evidence", "Check citations"],
          "open_docs": ["Search the web", "Read sources", "Open the documentation"],
          "news": ["Search news", "Check dates", "Read articles", "Analyse coverage", "Check citations"],
          "research": ["Plan research", "Search the web", "Read sources", "Fill gaps", "Analyse evidence",
                       "Check citations"],
          "factcheck": ["Find claims", "Search evidence", "Read sources", "Judge claims", "Check citations"]}


class Cancelled(Exception):
    pass


class BudgetExceeded(Exception):
    pass


_ids = itertools.count(1)


@dataclass
class ResearchTask:
    user_id: int
    session_key: str
    kind: str
    query: str
    deadline: float
    max_searches: int
    max_pages: int
    publish: Callable[["ResearchTask"], None]
    clock: Callable[[], float] = time.monotonic
    local_id: int = field(default_factory=lambda: next(_ids))
    research_id: int | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    fresh: bool = False                   # bypass the search cache (refreshing earlier research)
    status: str = "running"
    detail: str = ""
    searches: int = 0
    pages: int = 0
    steps: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.steps = [{"description": s, "status": "pending"} for s in STAGES.get(self.kind, STAGES["search"])]

    # -- checks ---------------------------------------------------------------------------------
    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()
        if self.clock() > self.deadline:
            raise BudgetExceeded("time")

    def remaining_s(self) -> float:
        return max(0.0, self.deadline - self.clock())

    def can_search(self) -> bool:
        return self.searches < self.max_searches

    def can_read(self) -> bool:
        return self.pages < self.max_pages

    # -- progress ---------------------------------------------------------------------------------
    def stage(self, name: str, detail: str = "") -> None:
        """Mark ``name`` running (earlier running stages become completed)."""
        for step in self.steps:
            if step["status"] == "running" and step["description"] != name:
                step["status"] = "completed"
        for step in self.steps:
            if step["description"] == name:
                step["status"] = "running"
        self.detail = detail
        self.publish(self)

    def note(self, detail: str) -> None:
        self.detail = detail
        self.publish(self)

    def finish(self, status: str) -> None:
        self.status = status
        for step in self.steps:
            if step["status"] == "running":
                step["status"] = "completed" if status in ("completed", "partial") else (
                    "cancelled" if status == "cancelled" else "failed")
            elif step["status"] == "pending":
                step["status"] = "skipped" if status in ("completed", "partial") else "cancelled"
        self.publish(self)

    def snapshot(self) -> dict:
        return {"id": self.research_id, "title": self.query[:120], "kind": self.kind, "status": self.status,
                "detail": self.detail, "searches": self.searches, "pages": self.pages,
                "steps": [dict(s) for s in self.steps], "label": "RESEARCHING"}


class ResearchTaskManager:
    def __init__(self, bus: EventBus, clock: Callable[[], float] = time.monotonic) -> None:
        self._bus = bus
        self._clock = clock
        self._tasks: dict[int, ResearchTask] = {}
        self._lock = threading.Lock()

    def start(self, user_id: int, session_key: str, kind: str, query: str, *, timeout_s: float, max_searches: int,
              max_pages: int) -> ResearchTask:
        task = ResearchTask(user_id, session_key, kind, query, self._clock() + timeout_s, max_searches, max_pages,
                            self._publish, self._clock)
        with self._lock:
            self._tasks[task.local_id] = task
        return task

    def done(self, task: ResearchTask) -> None:
        with self._lock:
            self._tasks.pop(task.local_id, None)

    def _publish(self, task: ResearchTask) -> None:
        self._bus.publish(Events.RESEARCH_PROGRESS, {"user_id": task.user_id, "task": task.snapshot()})

    def running(self, user_id: int | None = None) -> list[ResearchTask]:
        with self._lock:
            return [t for t in self._tasks.values() if user_id is None or t.user_id == user_id]

    def cancel(self, user_id: int | None = None, session_key: str | None = None) -> int:
        n = 0
        for task in self.running():
            if (user_id is None or task.user_id == user_id) and (session_key is None or task.session_key == session_key):
                task.cancel.set()
                n += 1
        return n
