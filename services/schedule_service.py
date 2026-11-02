"""Schedule events and the background reminder service."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.event_bus import EventBus, Events
from services.task_service import ValidationError, ceil_minute
from storage.local_database import LocalDatabase

log = logging.getLogger(__name__)

MAX_TITLE = 200
MAX_DESCRIPTION = 2000
MAX_REMINDER_MINUTES = 7 * 24 * 60
# A reminder that was missed (app not running) is still delivered if the event
# started less than this long ago; older ones are marked as skipped.
LATE_REMINDER_GRACE = timedelta(minutes=15)
# Reminders missed by more than the grace period (DUMBOO closed, nobody signed in) are still
# shown once, marked as missed, if they were due within this window.
MISSED_REMINDER_WINDOW = timedelta(hours=12)


@dataclass
class ScheduleEvent:
    id: int
    title: str
    description: str
    starts_at: datetime
    reminder_minutes: int | None
    reminded_at: datetime | None

    @classmethod
    def from_row(cls, row: dict) -> "ScheduleEvent":
        return cls(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            starts_at=datetime.fromisoformat(row["starts_at"]),
            reminder_minutes=row["reminder_minutes"],
            reminded_at=datetime.fromisoformat(row["reminded_at"]) if row["reminded_at"] else None,
        )

    @property
    def reminder_at(self) -> datetime | None:
        if self.reminder_minutes is None:
            return None
        return self.starts_at - timedelta(minutes=self.reminder_minutes)


def parse_start(day: date | str, at: time | str) -> datetime:
    """Combine a date (YYYY-MM-DD) and a time (HH:MM, 24h) into a datetime."""
    try:
        d = day if isinstance(day, date) else date.fromisoformat(str(day).strip())
    except ValueError as exc:
        raise ValidationError("Date must look like YYYY-MM-DD") from exc
    try:
        t = at if isinstance(at, time) else time.fromisoformat(str(at).strip())
    except ValueError as exc:
        raise ValidationError("Time must look like HH:MM (24-hour)") from exc
    return datetime.combine(d, t.replace(second=0, microsecond=0, tzinfo=None))


def _clean(title: str, description: str, reminder_minutes: int | None) -> tuple[str, str, int | None]:
    if not isinstance(title, str) or not title.strip():
        raise ValidationError("Event title is required")
    title = " ".join(title.split())
    if len(title) > MAX_TITLE:
        raise ValidationError(f"Event title must be at most {MAX_TITLE} characters")
    description = (description or "").strip()
    if len(description) > MAX_DESCRIPTION:
        raise ValidationError(f"Description must be at most {MAX_DESCRIPTION} characters")
    if reminder_minutes is not None:
        if isinstance(reminder_minutes, bool) or not isinstance(reminder_minutes, int):
            raise ValidationError("Reminder must be a whole number of minutes")
        if not 0 <= reminder_minutes <= MAX_REMINDER_MINUTES:
            raise ValidationError("Reminder must be between 0 minutes and 7 days before the event")
    return title, description, reminder_minutes


class ScheduleService:
    """Events belong to the signed-in user; every query is filtered by the user from the session."""

    def __init__(self, db: LocalDatabase, auth, clock=datetime.now) -> None:
        self._db = db
        self._auth = auth
        self._clock = clock

    def _now_iso(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    def _user(self, session) -> int:
        return self._auth.require(session, "tasks.manage").id

    def now(self) -> datetime:
        return self._clock()

    def create(self, session, title: str, starts_at: datetime, description: str = "",
               reminder_minutes: int | None = 10) -> ScheduleEvent:
        title, description, reminder_minutes = _clean(title, description, reminder_minutes)
        if not isinstance(starts_at, datetime):
            raise ValidationError("Start time is required")
        starts_at = ceil_minute(starts_at)
        user_id = self._user(session)
        now = self._now_iso()
        event_id = self._db.execute(
            "INSERT INTO events (user_id, title, description, starts_at, reminder_minutes, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, title, description, starts_at.isoformat(timespec="minutes"), reminder_minutes, now, now),
        )
        return self.get(session, event_id)

    def get(self, session, event_id: int) -> ScheduleEvent:
        rows = self._db.query("SELECT * FROM events WHERE id = ? AND user_id = ?", (int(event_id), self._user(session)))
        if not rows:
            raise KeyError(f"Event {event_id} not found")
        return ScheduleEvent.from_row(rows[0])

    def update(self, session, event_id: int, title: str, starts_at: datetime, description: str = "",
               reminder_minutes: int | None = 10) -> ScheduleEvent:
        title, description, reminder_minutes = _clean(title, description, reminder_minutes)
        starts_at = ceil_minute(starts_at)
        old = self.get(session, event_id)
        # Re-arm the reminder when the timing changes.
        reset = old.starts_at != starts_at or old.reminder_minutes != reminder_minutes
        self._db.execute(
            "UPDATE events SET title = ?, description = ?, starts_at = ?, reminder_minutes = ?, "
            "reminded_at = CASE WHEN ? THEN NULL ELSE reminded_at END, updated_at = ? WHERE id = ? AND user_id = ?",
            (title, description, starts_at.isoformat(timespec="minutes"), reminder_minutes,
             1 if reset else 0, self._now_iso(), int(event_id), self._user(session)),
        )
        return self.get(session, event_id)

    def delete(self, session, event_id: int) -> None:
        if not self._db.execute("DELETE FROM events WHERE id = ? AND user_id = ?", (int(event_id), self._user(session))):
            raise KeyError(f"Event {event_id} not found")

    def _between(self, session, start: datetime, end: datetime) -> list[ScheduleEvent]:
        rows = self._db.query(
            "SELECT * FROM events WHERE user_id = ? AND starts_at >= ? AND starts_at < ? ORDER BY starts_at",
            (self._user(session), start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")),
        )
        return [ScheduleEvent.from_row(r) for r in rows]

    def for_day(self, session, day: date) -> list[ScheduleEvent]:
        start = datetime.combine(day, time.min)
        return self._between(session, start, start + timedelta(days=1))

    def today(self, session) -> list[ScheduleEvent]:
        return self.for_day(session, self._clock().date())

    def upcoming(self, session, days: int = 30, include_today: bool = False) -> list[ScheduleEvent]:
        now = self._clock()
        start = now if include_today else datetime.combine(now.date() + timedelta(days=1), time.min)
        end = datetime.combine(now.date() + timedelta(days=days + 1), time.min)
        return self._between(session, start, end)

    def due_reminders(self, session) -> tuple[list[ScheduleEvent], list[ScheduleEvent]]:
        """Return (deliver_now, too_late_to_deliver) reminders of the signed-in user not yet handled."""
        now = self._clock()
        rows = self._db.query(
            "SELECT * FROM events WHERE user_id = ? AND reminder_minutes IS NOT NULL AND reminded_at IS NULL "
            "ORDER BY starts_at", (self._user(session),))
        deliver, stale = [], []
        for event in map(ScheduleEvent.from_row, rows):
            if event.reminder_at is None or event.reminder_at > now:
                continue
            (stale if now - event.starts_at > LATE_REMINDER_GRACE else deliver).append(event)
        return deliver, stale

    def mark_reminded(self, session, event_id: int) -> None:
        self._db.execute("UPDATE events SET reminded_at = ? WHERE id = ? AND user_id = ?",
                         (self._now_iso(), int(event_id), self._user(session)))


class ReminderService:
    """Polls the signed-in user's schedule and publishes ``REMINDER_DUE`` events.

    Reminders are private: only the user who is signed in gets theirs. Another
    user's reminders wait until that user signs in (and are delivered then if the
    event started less than 15 minutes ago). ``running`` reflects whether the
    polling thread is alive, so the UI never claims reminders are active when
    they are not.
    """

    name = "reminders"

    def __init__(self, schedule: ScheduleService, bus: EventBus, current_session=lambda: None,
                 interval_s: float = 15.0, tasks=None) -> None:
        self._schedule = schedule
        self._tasks = tasks   # TaskService: tasks with their own reminder time (Module 8 agenda commands)
        self._bus = bus
        self._current_session = current_session
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def check_now(self) -> int:
        session = self._current_session()
        if session is None:
            return 0
        try:
            deliver, stale = self._schedule.due_reminders(session)
        except Exception as exc:  # signed out meanwhile / no permission: nothing to deliver
            log.debug("Reminder check skipped: %s", exc.__class__.__name__)
            return 0
        now = self._schedule.now()
        for event in stale:
            self._schedule.mark_reminded(session, event.id)
            if now - event.starts_at > MISSED_REMINDER_WINDOW:
                log.info("Skipping old missed reminder for event %s", event.id)
                continue
            # DUMBOO was closed or you weren't signed in when it was due: still tell you, marked as missed.
            self._bus.publish(Events.REMINDER_DUE, {
                "event_id": event.id, "title": event.title, "kind": "event",
                "starts_at": event.starts_at.isoformat(timespec="minutes"), "missed": True,
            })
        for event in deliver:
            self._schedule.mark_reminded(session, event.id)
            self._bus.publish(Events.REMINDER_DUE, {
                "event_id": event.id,
                "title": event.title,
                "kind": "event",
                "starts_at": event.starts_at.isoformat(timespec="minutes"),
            })
        return len(deliver) + self._check_tasks(session, now)

    def _check_tasks(self, session, now: datetime) -> int:
        if self._tasks is None:
            return 0
        try:
            due = self._tasks.due_reminders(session, now)
        except Exception as exc:
            log.debug("Task reminder check skipped: %s", exc.__class__.__name__)
            return 0
        delivered = 0
        for task in due:
            self._tasks.mark_reminded(session, task.id)
            late = now - task.remind_at
            if late > MISSED_REMINDER_WINDOW:
                log.info("Skipping old missed reminder for task %s", task.id)
                continue
            payload = {"task_id": task.id, "title": task.title, "kind": "task",
                       "starts_at": (task.due_at or task.remind_at).isoformat(timespec="minutes"),
                       "has_due": task.due_date is not None, "due_time": task.due_time is not None}
            if late > LATE_REMINDER_GRACE:
                payload["missed"] = True
            else:
                delivered += 1
            self._bus.publish(Events.REMINDER_DUE, payload)
        return delivered

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_now()
            except Exception:
                log.exception("Reminder check failed")
            self._stop.wait(self._interval)

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="Reminders", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None


def reminder_speech(payload: dict, now: datetime) -> str:
    """What DUMBOO says when a reminder is due: what to do and when."""
    title = payload["title"]
    try:
        at = datetime.fromisoformat(payload["starts_at"])
    except (KeyError, ValueError):
        at = None
    clock = at.strftime("%I:%M %p").lstrip("0") if at else ""
    if payload.get("missed"):
        when = f", which was due at {clock}" if at else ""
        return f"You missed a reminder while I was away: {title}{when}."
    if title.strip().lower() in ("reminder", "your reminder", "a reminder"):
        return "Here's the reminder you asked for. Say snooze if you need more time."
    if payload.get("kind") == "task":
        text = f"Reminder: time to {title[:1].lower() + title[1:]}."
        if at is not None and payload.get("due_time") and at > now:
            minutes = round((at - now).total_seconds() / 60)
            text += f" It's due at {clock}" + (f", in {minutes} minutes." if minutes <= 90 else ".")
        elif payload.get("has_due") and at is not None and at.date() == now.date():
            text += " It's due today."
        return text + " Say done when it's finished, or snooze for more time."
    if at is not None and at - now > timedelta(minutes=1):
        minutes = round((at - now).total_seconds() / 60)
        soon = f"in {minutes} minutes" if minutes < 90 else f"at {clock}"
        return f"Reminder: {title} starts {soon}, at {clock}. Say snooze if you need more time."
    return f"Reminder: {title}. Say snooze if you need more time."


def reminder_banner(payload: dict) -> str:
    """The one-line dashboard text for a due reminder."""
    when = payload.get("starts_at", "").replace("T", " ")
    if payload.get("missed"):
        return f"Missed reminder: {payload['title']} (was due {when})"
    if payload.get("kind") == "task":
        due = f" · due {when}" if payload.get("has_due") else ""
        return f"Task: {payload['title']}{due} · say “done” when finished or “snooze”"
    return f"Reminder: {payload['title']} at {when} · say “snooze” for 10 more minutes"
