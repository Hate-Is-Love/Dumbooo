"""Tasks, schedule, reminders and "what do I have to do" by voice and text, including slang (AgendaAssistant)."""

import json
from datetime import date, datetime, time, timedelta

import pytest

from ai.agenda_slang import normalize
from app.event_bus import Events
from services.schedule_service import reminder_banner, reminder_speech
from tests.everyday_fakes import FakeWeatherHTTP
from tests.fakes import wait_for
from tests.test_history_memory import Rig

NOW = datetime(2026, 10, 22, 14, 0)  # a Thursday afternoon


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def rig(paths):
    r = Rig(paths, weather_client=FakeWeatherHTTP())
    r.clock = Clock(NOW)
    for part in (r.core.agenda_assistant, r.core.everyday_assistant, r.core.schedule, r.core.tasks):
        part._clock = r.clock
    yield r
    r.close()


def tasks(rig, done=False):
    return rig.core.tasks.list(rig.session, done)


def events(rig):
    return rig.core.schedule.upcoming(rig.session, days=60, include_today=True)


# -- slang is understood ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("said,plain", [
    ("yo ping me tmrw at 5 to hit the gym", "remind me tomorrow at 5 to hit the gym"),
    ("put milk on my list pls", "add a task milk"),
    ("whats on my plate today", "what do i have to do today"),
    ("bro what am i doing tomorrow", "what do i have to do tomorrow"),
    ("knocked out the report", "mark the report as done"),
    ("the assignment is done", "mark the assignment as done"),
    ("bump the dentist to friday", "move the dentist to friday"),
    ("scrap the meeting with rahul", "cancel the meeting with rahul"),
    ("gimme a heads up in 20 mins to check the oven", "remind me in 20 minutes to check the oven"),
    ("dont let me forget to pay rent by friday", "remind me to pay rent by friday"),
    ("pencil in a dentist appt on friday at 4", "schedule a dentist appointment on friday at 4"),
    ("kal 5 baje yaad dila dena", "remind me tomorrow at 5"),
    ("aaj kya karna hai", "what do i have to do today"),
    ("what is the capital of france", "what is the capital of france"),   # normal questions stay as they are
    ("open youtube", "open youtube"),
])
def test_slang_becomes_a_plain_command(said, plain):
    assert normalize(said) == plain


# -- tasks ----------------------------------------------------------------------------------------------------------
def test_a_task_with_a_deadline_is_saved_in_tasks_with_its_own_reminder(rig):
    reply = rig.say("add a task to submit the report by Friday at 5 pm", source="voice")
    [task] = tasks(rig)
    assert (task.title, task.due_date, task.due_time) == ("Submit the report", date(2026, 10, 23), time(17, 0))
    assert task.remind_at == datetime(2026, 10, 23, 16, 30)          # 30 minutes before the deadline
    assert "Added to **TASKS**" in reply["text"] and "due tomorrow at 5:00 PM" in reply["text"]
    assert "I'll remind you tomorrow at 4:30 PM" in reply["speech"]
    assert rig.provider.requests == []                                 # understood locally, no AI call

    listed = rig.say("what are my tasks", source="voice")
    assert "Submit the report" in listed["text"] and "due tomorrow at 5:00 PM" in listed["speech"]

    assert "Marked **Submit the report** as done" in rig.reply("mark the report as done")
    assert tasks(rig) == [] and tasks(rig, True)[0].title == "Submit the report"
    assert "Undone" in rig.reply("undo")
    assert tasks(rig)[0].title == "Submit the report"


def test_slang_task_commands_work_end_to_end(rig):
    assert "Added to **TASKS**: **Milk**" in rig.reply("yo put milk on my list pls")
    assert "Call the bank" in rig.reply("jot down call the bank")
    assert "Marked **Milk** as done" in rig.reply("knocked out milk")
    assert "Deleted the task **Call the bank**" in rig.reply("scrap the task call the bank")
    assert tasks(rig) == []
    rig.reply("add a task to clean my room")
    assert tasks(rig)[0].title == "Clean my room"                      # from your point of view, not "your room"
    assert "high priority" in rig.reply("make clean my room high priority")
    assert tasks(rig)[0].priority == "high"


def test_a_date_only_deadline_reminds_that_morning_and_overdue_tasks_are_flagged(rig):
    rig.reply("i need to renew the car insurance by monday")
    [task] = tasks(rig)
    assert task.due_date == date(2026, 10, 26) and task.due_time is None
    assert task.remind_at == datetime(2026, 10, 26, 9, 0)
    rig.core.tasks.create(rig.session, "Return library books", due_date=date(2026, 10, 20))
    reply = rig.say("anything overdue")
    assert "Return library books" in reply["text"] and "Renew" not in reply["text"]
    assert "**OVERDUE**" in reply["text"]


def test_moving_a_task_moves_its_reminder_too(rig):
    rig.reply("add a task to submit the report by Friday at 5 pm")
    reply = rig.reply("move submit the report to Monday")
    task = tasks(rig)[0]
    assert task.due_date == date(2026, 10, 26) and task.due_time == time(17, 0)
    assert task.remind_at == datetime(2026, 10, 26, 16, 30) and "is now due on Monday" in reply


# -- schedule --------------------------------------------------------------------------------------------------------
def test_schedule_commands_by_voice(rig):
    reply = rig.say("schedule a meeting with Rahul tomorrow at 3, remind me 30 minutes before", source="voice")
    [event] = events(rig)
    assert (event.title, event.starts_at, event.reminder_minutes) == (
        "Meeting with Rahul", datetime(2026, 10, 23, 15, 0), 30)
    assert "Added to your **SCHEDULE**" in reply["text"] and "30 minutes before" in reply["speech"]

    assert "3:00 PM" in rig.reply("what's on my schedule tomorrow")
    assert "Moved **Meeting with Rahul** to tomorrow at 5:00 PM" in rig.reply("move the meeting with rahul to 5 pm")
    assert events(rig)[0].starts_at == datetime(2026, 10, 23, 17, 0)
    assert "Not quite" in rig.reply("am I free tomorrow at 5 pm")
    assert "you're free" in rig.reply("am I free tomorrow at 11 am")
    assert "tomorrow at 5:00 PM" in rig.reply("when is my meeting with rahul")
    assert "Cancelled **Meeting with Rahul**" in rig.reply("scrap the meeting with rahul")
    assert events(rig) == []
    assert rig.provider.requests == []


def test_an_event_without_a_time_is_asked_for(rig):
    reply = rig.say("schedule a dentist appointment", source="voice")
    assert "when is **Dentist appointment**" in reply["text"] and reply["listen_after"]
    assert "Added to your **SCHEDULE**" in rig.reply("friday at 4 pm")
    assert events(rig)[0].starts_at == datetime(2026, 10, 23, 16, 0)


def test_hinglish_reminder_is_saved(rig):
    rig.reply("mujhe kal subah 7 baje gym jaana hai yaad dilana")
    [event] = events(rig)
    assert event.starts_at == datetime(2026, 10, 23, 7, 0) and "gym" in event.title.lower()


# -- reminders go where they belong ------------------------------------------------------------------------------------
def test_reminders_are_saved_in_the_right_section(rig):
    reply = rig.reply("remind me to pay rent by monday")          # a to-do with a deadline -> TASKS
    assert "TASKS" in reply and tasks(rig)[0].title == "Pay rent" and events(rig) == []

    reply = rig.reply("remind me to call mom at 5")                # a plain timed reminder -> SCHEDULE
    assert "SCHEDULE" in reply and events(rig)[0].title == "Call mom"

    rig.reply("add a task to water the plants")                     # reminding about an existing task: no duplicate
    reply = rig.reply("remind me to water the plants at 6 pm")
    plants = next(t for t in tasks(rig) if t.title == "Water the plants")
    assert plants.remind_at == datetime(2026, 10, 22, 18, 0) and "Saved on that task" in reply
    assert len(events(rig)) == 1


# -- "what do I have to do" ---------------------------------------------------------------------------------------------
def test_agenda_lists_everything_with_its_time(rig):
    s = rig.session
    rig.core.schedule.create(s, "Team sync", datetime(2026, 10, 22, 16, 0))
    rig.core.schedule.create(s, "Call mom", datetime(2026, 10, 22, 18, 0), description="Set by DUMBOO",
                             reminder_minutes=0)
    rig.core.tasks.create(s, "Send the invoice", due_date=date(2026, 10, 22), due_time=time(17, 0))
    rig.core.tasks.create(s, "Return library books", due_date=date(2026, 10, 20))
    rig.core.tasks.create(s, "Learn guitar")
    rig.core.schedule.create(s, "Tomorrow thing", datetime(2026, 10, 23, 10, 0))

    reply = rig.say("whats on my plate today", source="voice")
    text, speech = reply["text"], reply["speech"]
    assert text.index("4:00 PM") < text.index("5:00 PM") < text.index("6:00 PM")   # in time order
    assert "Team sync" in text and "reminder to Call mom" in text and "Send the invoice (task due)" in text
    assert "**Overdue:** Return library books" in text and "Learn guitar" in text
    assert "Tomorrow thing" not in text
    assert speech.startswith("Here's what you have left today: at 4:00 PM, Team sync")
    assert "Next up: **Team sync** today at 4:00 PM" in rig.reply("what's next")
    assert "Tomorrow thing" in rig.reply("what do i have to do tomorrow")


# -- task reminders are delivered, spoken, completed or snoozed -----------------------------------------------------------
def test_task_reminder_is_delivered_and_done_or_snooze_follow_it(rig):
    rig.reply("add a task to send the invoice by 5 pm")
    task = tasks(rig)[0]
    assert task.remind_at == datetime(2026, 10, 22, 16, 30)
    rig.clock.now = datetime(2026, 10, 22, 16, 31)
    assert rig.core.reminders.check_now() == 1
    assert wait_for(lambda: rig.rec.of(Events.REMINDER_DUE))
    due = rig.rec.of(Events.REMINDER_DUE)[-1]
    assert due["kind"] == "task" and due["task_id"] == task.id
    speech = reminder_speech(due, rig.clock.now)
    assert speech.startswith("Reminder: time to send the invoice. It's due at 5:00 PM, in 29 minutes.")
    assert "Task: Send the invoice" in reminder_banner(due)
    assert rig.core.reminders.check_now() == 0                      # delivered once

    wait_for(lambda: rig.core.agenda_assistant._recent_task_reminder(rig.core.auth.require(rig.session).id))
    assert "back again" not in (reply := rig.reply("snooze for 15 minutes"))
    assert "again" in reply and tasks(rig)[0].remind_at == datetime(2026, 10, 22, 16, 46)
    assert "Send the invoice** as done" in rig.reply("done")
    assert tasks(rig) == []


# -- normal conversation is not taken over ----------------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["make a presentation on climate change", "plan a trip to goa for 3 days",
                                  "what is the capital of france", "I have a meeting with my boss and I'm nervous",
                                  "how do I schedule a meeting in Outlook"])
def test_normal_requests_still_go_to_the_ai(rig, text):
    rig.reply(text)
    assert tasks(rig) == [] and events(rig) == []
    assert rig.provider.requests and rig.provider.requests[-1].messages[-1].content == text


def test_the_ai_reads_unusual_phrasing_as_a_command(rig):
    rig.provider.answer = json.dumps({"action": "add_task", "title": "Passport renewal", "when": "2026-10-30T00:00",
                                      "time_given": False, "priority": "high", "range": None})
    reply = rig.reply("my passport renewal really needs sorting before the 30th, stick that in my tasks somehow")
    [task] = tasks(rig)
    assert (task.title, task.due_date, task.priority) == ("Passport renewal", date(2026, 10, 30), "high")
    assert "Added to **TASKS**" in reply


def test_other_users_never_see_your_tasks(rig):
    rig.reply("add a task to buy a gift")
    user = rig.core.auth.require(rig.session)
    rows = rig.core.db.query("SELECT user_id FROM tasks")
    assert {r["user_id"] for r in rows} == {user.id}


def test_tasks_page_dialog_sets_due_time_and_reminder(rig):
    from PySide6.QtCore import QDate, QDateTime, QTime
    from PySide6.QtWidgets import QApplication, QWidget

    from dashboard.widgets.tasks_panel import TaskDialog

    QApplication.instance() or QApplication([])
    parent = QWidget()   # kept alive: the dialog is deleted with its parent
    dialog = TaskDialog(parent)
    dialog.title.setText("Pay electricity bill")
    dialog.has_due.setChecked(True)
    dialog.due.setDate(QDate(2026, 10, 24))
    dialog.has_time.setChecked(True)
    dialog.due_time.setTime(QTime(18, 0))
    dialog.has_remind.setChecked(True)
    dialog.remind.setDateTime(QDateTime(QDate(2026, 10, 24), QTime(17, 0)))
    task = rig.core.tasks.create(rig.session, **dialog.values())
    assert (task.due_date, task.due_time, task.remind_at) == (date(2026, 10, 24), time(18, 0),
                                                              datetime(2026, 10, 24, 17, 0))
    edit = TaskDialog(parent, task)                         # editing shows the saved values
    assert edit.has_time.isChecked() and edit.has_remind.isChecked()
    assert edit.values()["remind_at"] == datetime(2026, 10, 24, 17, 0)
