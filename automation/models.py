"""Structured task representation: tasks are lists of steps, steps are capability calls.

Nothing in here is natural language that gets executed: a ``Step`` names a
registered capability and carries a dict of arguments that the policy engine
validates before the step runs.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"low": 0, "medium": 1, "high": 2}[self.value]

    @staticmethod
    def max(*risks: "Risk") -> "Risk":
        return max(risks, key=lambda r: r.rank)


class TaskStatus(str, Enum):
    PENDING = "pending"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def finished(self) -> bool:
        return self in (TaskStatus.COMPLETED, TaskStatus.PARTIALLY_COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)


class StepStatus(str, Enum):
    PENDING = "pending"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    RUNNING = "running"
    COMPLETED = "completed"     # ran and the result was verified
    UNVERIFIED = "unverified"   # ran, but the result could not be confirmed
    FAILED = "failed"
    DENIED = "denied"           # refused by the policy engine or by the user
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


@dataclass
class ActionResult:
    """What a capability handler reports. ``verified`` must come from an independent check."""

    ok: bool
    message: str
    verified: bool = False
    verification: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    changed: list[str] = field(default_factory=list)       # exact files/folders created or changed
    followups: list["Step"] = field(default_factory=list)  # steps to insert next (e.g. preview -> apply)
    undo: list[dict] = field(default_factory=list)         # reversible operations (e.g. moves)

    @classmethod
    def fail(cls, message: str, **data: Any) -> "ActionResult":
        return cls(False, message, data=data)

    @classmethod
    def done(cls, message: str, verification: str, **data: Any) -> "ActionResult":
        return cls(True, message, verified=True, verification=verification, data=data)


_step_ids = itertools.count(1)


@dataclass
class Step:
    capability: str
    args: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    id: int = field(default_factory=lambda: next(_step_ids))
    status: StepStatus = StepStatus.PENDING
    risk: Risk | None = None          # set by the policy engine, never by the planner
    approved: bool = False            # set only by a user confirmation
    confirm_details: str = ""         # shown with the confirmation question (previews, file lists, diffs)
    message: str = ""
    verification: str = ""
    error: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    changed: list[str] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "capability": self.capability, "description": self.description,
                "status": self.status.value, "risk": self.risk.value if self.risk else None,
                "message": self.message, "verification": self.verification, "error": self.error,
                "changed": list(self.changed[:50]), "started_at": self.started_at, "finished_at": self.finished_at}


@dataclass
class Task:
    request: str
    steps: list[Step]
    user_id: int
    source: str = "typed"
    origin: str = "rules"             # rules | planner | workflow | resume
    title: str = ""
    id: int | None = None             # database id once saved
    status: TaskStatus = TaskStatus.PENDING
    plan_approved: bool = False       # an AI-proposed plan needs approval as a whole first
    confirmation: str = "not_required"  # not_required | requested | approved | denied | expired
    summary: str = ""
    error: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    started_at: str | None = None
    finished_at: str | None = None
    undo: list[dict] = field(default_factory=list)
    session_key: str = ""             # hash of the owning session's token (never the token itself)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    decision_event: threading.Event = field(default_factory=threading.Event, repr=False)
    decision: bool | None = None
    awaiting_step: Step | None = None
    changed_event: threading.Condition = field(default_factory=threading.Condition, repr=False)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.steps:
            out[s.status.value] = out.get(s.status.value, 0) + 1
        return out

    def snapshot(self) -> dict[str, Any]:
        """Safe, JSON-friendly view for events and the dashboard (no session, no secrets)."""
        awaiting = self.awaiting_step
        return {"id": self.id, "title": self.title or self.request[:80], "request": self.request,
                "status": self.status.value, "source": self.source, "origin": self.origin,
                "confirmation": self.confirmation, "summary": self.summary, "error": self.error,
                "steps": [s.summary() for s in self.steps], "created_at": self.created_at,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "awaiting": None if awaiting is None else {
                    "step_id": awaiting.id, "description": awaiting.description,
                    "risk": awaiting.risk.value if awaiting.risk else "medium", "details": awaiting.confirm_details,
                    "plan": not self.plan_approved and self.origin in ("planner", "workflow", "resume")}}
