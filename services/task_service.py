"""Task management backed by SQLite."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

from storage.local_database import LocalDatabase

if TYPE_CHECKING:
    from services.auth_service import AuthService

PRIORITIES = ("low", "medium", "high")
_PRIORITY_ORDER = "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END"
MAX_TITLE = 200
MAX_NOTES = 2000


class ValidationError(ValueError):
    pass


@dataclass
class Task:
    id: int
    title: str
    notes: str
    priority: str
    due_date: date | None
    completed: bool
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    due_time: time | None = None           # optional time on the due date ("by Friday 5 pm")
    remind_at: datetime | None = None      # when DUMBOO reminds you (spoken + shown)
    reminded_at: datetime | None = None    # set once that reminder was delivered

    @classmethod
    def from_row(cls, row: dict) -> "Task":
        return cls(
            id=row["id"],
            title=row["title"],
            notes=row["notes"],
            priority=row["priority"],
            due_date=date.fromisoformat(row["due_date"]) if row["due_date"] else None,
            completed=bool(row["completed"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
            due_time=time.fromisoformat(row["due_time"]) if row.get("due_time") else None,
            remind_at=datetime.fromisoformat(row["remind_at"]) if row.get("remind_at") else None,
            reminded_at=datetime.fromisoformat(row["reminded_at"]) if row.get("reminded_at") else None,
        )

    @property
    def due_at(self) -> datetime | None:
        """The due moment: the due time on the due date, or the end of that day."""
        if self.due_date is None:
            return None
        return datetime.combine(self.due_date, self.due_time or time(23, 59))

    def is_overdue(self, today: date | None = None) -> bool:
        if self.completed or self.due_date is None:
            return False
        if self.due_time is not None and isinstance(today, datetime):
            return self.due_at < today
        today = today.date() if isinstance(today, datetime) else (today or date.today())
        return self.due_date < today


def _clean_title(title: str) -> str:
    if not isinstance(title, str) or not title.strip():
        raise ValidationError("Task title is required")
    title = " ".join(title.split())
    if len(title) > MAX_TITLE:
        raise ValidationError(f"Task title must be at most {MAX_TITLE} characters")
    return title


def _clean_notes(notes: str) -> str:
    notes = (notes or "").strip()
    if len(notes) > MAX_NOTES:
        raise ValidationError(f"Notes must be at most {MAX_NOTES} characters")
    return notes


def _clean_priority(priority: str) -> str:
    if priority not in PRIORITIES:
        raise ValidationError(f"Priority must be one of {', '.join(PRIORITIES)}")
    return priority


def _clean_time(value: time | str | None) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        try:
            value = time.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValidationError("Due time must look like HH:MM (24-hour)") from exc
    if not isinstance(value, time):
        raise ValidationError("Invalid due time")
    return value.replace(second=0, microsecond=0, tzinfo=None).isoformat(timespec="minutes")


def _clean_remind(value: datetime | str | None) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValidationError("Reminder must be a date and time") from exc
    if not isinstance(value, datetime):
        raise ValidationError("Invalid reminder time")
    return ceil_minute(value).replace(tzinfo=None).isoformat(timespec="minutes")


def ceil_minute(at: datetime) -> datetime:
    """Round up to a whole minute: a reminder "in 2 minutes" set at 11:21:52 is due at 11:24, never early."""
    whole = at.replace(second=0, microsecond=0)
    return whole if whole == at else whole + timedelta(minutes=1)


def _clean_due(due: date | str | None) -> str | None:
    if due in (None, ""):
        return None
    if isinstance(due, datetime):
        due = due.date()
    if isinstance(due, str):
        try:
            due = date.fromisoformat(due.strip())
        except ValueError as exc:
            raise ValidationError("Due date must look like YYYY-MM-DD") from exc
    if not isinstance(due, date):
        raise ValidationError("Invalid due date")
    return due.isoformat()


class TaskService:
    """Tasks belong to the signed-in user; every query is filtered by the user from the session."""

    def __init__(self, db: LocalDatabase, auth: "AuthService", clock=datetime.now) -> None:
        self._db = db
        self._auth = auth
        self._clock = clock

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    def _user(self, session) -> int:
        return self._auth.require(session, "tasks.manage").id

    def create(self, session, title: str, notes: str = "", priority: str = "medium",
               due_date: date | str | None = None, due_time: time | str | None = None,
               remind_at: datetime | str | None = None) -> Task:
        user_id = self._user(session)
        now = self._now()
        due = _clean_due(due_date)
        task_id = self._db.execute(
            "INSERT INTO tasks (user_id, title, notes, priority, due_date, due_time, remind_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, _clean_title(title), _clean_notes(notes), _clean_priority(priority), due,
             _clean_time(due_time) if due else None, _clean_remind(remind_at), now, now),
        )
        return self.get(session, task_id)

    def get(self, session, task_id: int) -> Task:
        rows = self._db.query("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (int(task_id), self._user(session)))
        if not rows:
            raise KeyError(f"Task {task_id} not found")
        return Task.from_row(rows[0])

    def update(self, session, task_id: int, *, title: str | None = None, notes: str | None = None,
               priority: str | None = None, due_date: date | str | None = ...,
               due_time: time | str | None = ..., remind_at: datetime | str | None = ...) -> Task:
        fields: dict[str, object] = {}
        if title is not None:
            fields["title"] = _clean_title(title)
        if notes is not None:
            fields["notes"] = _clean_notes(notes)
        if priority is not None:
            fields["priority"] = _clean_priority(priority)
        if due_date is not ...:
            fields["due_date"] = _clean_due(due_date)
            if fields["due_date"] is None:
                fields["due_time"] = None
        if due_time is not ...:
            fields["due_time"] = _clean_time(due_time)
        old = self.get(session, task_id)  # raises KeyError if missing or not yours
        if remind_at is not ...:
            fields["remind_at"] = _clean_remind(remind_at)
            old_remind = old.remind_at.isoformat(timespec="minutes") if old.remind_at else None
            if fields["remind_at"] != old_remind:
                fields["reminded_at"] = None  # a new reminder time is delivered again
        if fields:
            fields["updated_at"] = self._now()
            assignments = ", ".join(f"{name} = :{name}" for name in fields)  # names are fixed above
            self._db.execute(f"UPDATE tasks SET {assignments} WHERE id = :id AND user_id = :user_id",
                             {**fields, "id": int(task_id), "user_id": self._user(session)})
        return self.get(session, task_id)

    def set_completed(self, session, task_id: int, completed: bool) -> Task:
        now = self._now()
        changed = self._db.execute(
            "UPDATE tasks SET completed = ?, completed_at = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (1 if completed else 0, now if completed else None, now, int(task_id), self._user(session)),
        )
        if not changed:
            raise KeyError(f"Task {task_id} not found")
        return self.get(session, task_id)

    def delete(self, session, task_id: int) -> None:
        if not self._db.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (int(task_id), self._user(session))):
            raise KeyError(f"Task {task_id} not found")

    def list(self, session, completed: bool | None = None) -> list[Task]:
        where, params = "WHERE user_id = ?", [self._user(session)]
        if completed is not None:
            where += " AND completed = ?"
            params.append(1 if completed else 0)
        rows = self._db.query(
            f"SELECT * FROM tasks {where} ORDER BY completed, due_date IS NULL, due_date, {_PRIORITY_ORDER}, id",
            params,
        )
        return [Task.from_row(r) for r in rows]

    def due_reminders(self, session, now: datetime) -> list[Task]:
        """Pending tasks whose reminder time has come and that weren't reminded yet."""
        rows = self._db.query(
            "SELECT * FROM tasks WHERE user_id = ? AND completed = 0 AND remind_at IS NOT NULL AND reminded_at IS NULL "
            "AND remind_at <= ? ORDER BY remind_at", (self._user(session), now.isoformat(timespec="minutes")))
        return [Task.from_row(r) for r in rows]

    def mark_reminded(self, session, task_id: int) -> None:
        self._db.execute("UPDATE tasks SET reminded_at = ? WHERE id = ? AND user_id = ?",
                         (self._now(), int(task_id), self._user(session)))

    def snooze(self, session, task_id: int, minutes: int) -> Task:
        at = self._clock() + timedelta(minutes=max(1, int(minutes)))
        return self.update(session, task_id, remind_at=at)

    def counts(self, session) -> dict[str, int]:
        rows = self._db.query("SELECT completed, COUNT(*) AS n FROM tasks WHERE user_id = ? GROUP BY completed",
                              (self._user(session),))
        result = {"pending": 0, "completed": 0}
        for r in rows:
            result["completed" if r["completed"] else "pending"] = r["n"]
        return result
