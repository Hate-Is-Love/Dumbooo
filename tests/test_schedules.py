from datetime import date, datetime, timedelta

import pytest

from app.event_bus import EventBus, Events
from services.schedule_service import ReminderService, ScheduleService, parse_start
from services.task_service import ValidationError
from tests.fakes import RecordingBus


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def setup(accounts):
    clock = Clock(datetime(2026, 10, 21, 9, 0))
    return ScheduleService(accounts.db, accounts.auth, clock=clock), clock, accounts.owner


def test_parse_start_validates():
    assert parse_start("2026-10-21", "14:05") == datetime(2026, 10, 21, 14, 5)
    for day, at in (("2026-13-01", "10:00"), ("21-10-2026", "10:00"), ("2026-10-21", "25:00"), ("2026-10-21", "noon")):
        with pytest.raises(ValidationError):
            parse_start(day, at)


def test_crud(setup):
    schedule, _clock, s = setup
    e = schedule.create(s, "Standup", datetime(2026, 10, 21, 10, 0), "daily sync", reminder_minutes=5)
    assert e.reminder_at == datetime(2026, 10, 21, 9, 55)
    e = schedule.update(s, e.id, "Standup (moved)", datetime(2026, 10, 21, 11, 0), "", reminder_minutes=None)
    assert e.title == "Standup (moved)" and e.reminder_minutes is None and e.reminder_at is None
    schedule.delete(s, e.id)
    with pytest.raises(KeyError):
        schedule.get(s, e.id)


@pytest.mark.parametrize("kwargs", [
    {"title": ""}, {"title": "x" * 201}, {"title": "ok", "reminder_minutes": -1},
    {"title": "ok", "reminder_minutes": 99999}, {"title": "ok", "reminder_minutes": 2.5},
    {"title": "ok", "description": "d" * 2001},
])
def test_validation(setup, kwargs):
    schedule, _, s = setup
    with pytest.raises(ValidationError):
        schedule.create(s, starts_at=datetime(2026, 10, 22, 9, 0), **kwargs)


def test_today_and_upcoming(setup):
    schedule, _clock, s = setup
    schedule.create(s, "today early", datetime(2026, 10, 21, 7, 0))
    schedule.create(s, "today late", datetime(2026, 10, 21, 23, 59))
    schedule.create(s, "tomorrow", datetime(2026, 10, 22, 8, 0))
    schedule.create(s, "in 40 days", datetime(2026, 11, 30, 8, 0))
    schedule.create(s, "yesterday", datetime(2026, 10, 20, 8, 0))
    assert [e.title for e in schedule.today(s)] == ["today early", "today late"]
    assert [e.title for e in schedule.upcoming(s, 30)] == ["tomorrow"]
    assert [e.title for e in schedule.for_day(s, date(2026, 10, 20))] == ["yesterday"]


def test_reminders_fire_once_at_the_right_time(setup):
    schedule, clock, s = setup
    bus = EventBus(synchronous=True)
    rec = RecordingBus(bus)
    reminders = ReminderService(schedule, bus, lambda: s)
    schedule.create(s, "Call mum", datetime(2026, 10, 21, 9, 30), reminder_minutes=10)

    clock.now = datetime(2026, 10, 21, 9, 19)
    assert reminders.check_now() == 0
    clock.now = datetime(2026, 10, 21, 9, 20)
    assert reminders.check_now() == 1
    assert rec.of(Events.REMINDER_DUE)[0]["title"] == "Call mum"
    clock.now += timedelta(minutes=1)
    assert reminders.check_now() == 0  # never twice


def test_missed_reminders_are_delivered_once_marked_missed_or_skipped_when_old(setup):
    schedule, clock, s = setup
    bus = EventBus(synchronous=True)
    rec = RecordingBus(bus)
    schedule.create(s, "Days ago", datetime(2026, 10, 20, 6, 0), reminder_minutes=5)       # > 12 h: skipped
    schedule.create(s, "Missed this morning", datetime(2026, 10, 21, 6, 0), reminder_minutes=5)
    schedule.create(s, "Just started", datetime(2026, 10, 21, 8, 55), reminder_minutes=5)
    assert ReminderService(schedule, bus, lambda: s).check_now() == 1
    due = rec.of(Events.REMINDER_DUE)
    assert [(p["title"], p.get("missed", False)) for p in due] == [("Missed this morning", True), ("Just started", False)]
    assert schedule.due_reminders(s) == ([], [])
    ReminderService(schedule, bus, lambda: s).check_now()
    assert len(rec.of(Events.REMINDER_DUE)) == 2  # never repeated


def test_rescheduling_rearms_reminder(setup):
    schedule, clock, s = setup
    bus = EventBus(synchronous=True)
    e = schedule.create(s, "Meeting", datetime(2026, 10, 21, 9, 5), reminder_minutes=10)
    ReminderService(schedule, bus, lambda: s).check_now()
    assert schedule.get(s, e.id).reminded_at is not None
    e = schedule.update(s, e.id, "Meeting", datetime(2026, 10, 21, 15, 0), "", reminder_minutes=10)
    assert e.reminded_at is None


def test_reminders_only_for_the_signed_in_user(setup, accounts):
    schedule, clock, owner = setup
    bus = EventBus(synchronous=True)
    rec = RecordingBus(bus)
    schedule.create(owner, "Owner's dentist", datetime(2026, 10, 21, 9, 5), reminder_minutes=10)
    assert ReminderService(schedule, bus, lambda: accounts.user).check_now() == 0      # Bob signed in
    assert ReminderService(schedule, bus, lambda: None).check_now() == 0               # nobody signed in
    assert rec.of(Events.REMINDER_DUE) == []
    assert ReminderService(schedule, bus, lambda: owner).check_now() == 1              # owner gets it


def test_events_are_private_per_user(setup, accounts):
    schedule, _clock, owner = setup
    e = schedule.create(owner, "Private event", datetime(2026, 10, 22, 9, 0))
    assert schedule.upcoming(accounts.user, 30) == []
    with pytest.raises(KeyError):
        schedule.get(accounts.user, e.id)
    with pytest.raises(KeyError):
        schedule.delete(accounts.user, e.id)


def test_reminder_thread_starts_and_stops(setup):
    schedule, _, s = setup
    service = ReminderService(schedule, EventBus(synchronous=True), lambda: s, interval_s=0.05)
    service.start()
    service.start()  # no duplicate thread
    assert service.running
    service.stop()
    assert not service.running
