"""Time, weather, voice reminders, translation and conversation recall (EverydayAssistant), end to end."""

import json
from datetime import datetime, timedelta

import pytest

from app.event_bus import Events
from services.time_parse import describe, parse_when
from tests.everyday_fakes import FakeWeatherHTTP
from tests.fakes import wait_for
from tests.test_history_memory import Rig

NOW = datetime(2026, 10, 22, 14, 0)  # a Thursday


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def rig(paths):
    r = Rig(paths, weather_client=FakeWeatherHTTP())
    r.clock = Clock(NOW)
    r.core.everyday_assistant._clock = r.clock
    r.core.schedule._clock = r.clock
    yield r
    r.close()


def reminders(rig):
    return [e for e in rig.core.schedule.upcoming(rig.session, days=30, include_today=True) if e.reminder_minutes == 0]


# -- time parsing ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected,title", [
    ("remind me to call mom at 5", datetime(2026, 10, 22, 17, 0), "call mom"),
    ("remind me in 10 minutes to drink water", datetime(2026, 10, 22, 14, 10), "drink water"),
    ("set a reminder for 6 pm to call mom", datetime(2026, 10, 22, 18, 0), "call mom"),
    ("remind me tomorrow at 9 am about the exam", datetime(2026, 10, 23, 9, 0), "the exam"),
    ("remind me to submit the assignment on Friday at 3:30 pm", datetime(2026, 10, 23, 15, 30), "submit the assignment"),
    ("remind me at 8 tomorrow morning to jog", datetime(2026, 10, 23, 8, 0), "jog"),
    ("remind me after half an hour to check the oven", datetime(2026, 10, 22, 14, 30), "check the oven"),
    ("remind me on 25 October at 10 to pay rent", datetime(2026, 10, 25, 10, 0), "pay rent"),
    ("remind me at 1 to join the meeting", datetime(2026, 10, 23, 13, 0), "join the meeting"),  # 1 pm, not 1 am
    ("remind me tonight to take my medicine", datetime(2026, 10, 22, 20, 0), "take your medicine"),
    ("wake me up at 6:30 am", datetime(2026, 10, 23, 6, 30), ""),
    ("remind me next Monday to renew the subscription", datetime(2026, 10, 26, 9, 0), "renew the subscription"),
])
def test_spoken_times_are_understood(text, expected, title):
    when = parse_when(text, NOW)
    assert (when.at, when.title) == (expected, title)


def test_no_time_means_no_guess():
    assert parse_when("remind me to buy milk", NOW) is None
    assert describe(NOW + timedelta(minutes=10), NOW) == "in 10 minutes (at 2:10 PM)"


# -- time and date -------------------------------------------------------------------------------------------
def test_time_and_date_are_answered_locally_without_the_ai(rig):
    assert "2:00 PM" in rig.reply("Hey DUMBOO, what time is it?")
    assert "Thursday, 22 October 2026" in rig.reply("what's the date today")
    assert "London" in rig.reply("what time is it in London")
    assert rig.provider.requests == []  # no AI call, no quota used


# -- reminders by voice and text ---------------------------------------------------------------------------------
def test_voice_reminder_is_saved_listed_and_cancelled(rig):
    reply = rig.say("remind me to call mom at 5", source="voice")
    assert "call mom" in reply["text"] and "today at 5:00 PM" in reply["text"]
    assert "remind you to call mom today at 5:00 PM" in reply["speech"]
    [event] = reminders(rig)
    assert (event.title, event.starts_at) == ("Call mom", datetime(2026, 10, 22, 17, 0))  # saved in the database
    assert "Call mom" in rig.reply("what are my reminders")
    assert "cancelled the reminder to **Call mom**" in rig.reply("cancel the reminder about mom")
    assert reminders(rig) == []
    assert rig.provider.requests == []


def test_missing_time_is_asked_for(rig):
    assert "when should I remind you to buy milk" in rig.reply("remind me to buy milk")
    assert "in 20 minutes" in rig.reply("in 20 minutes")
    [event] = reminders(rig)
    assert event.starts_at == NOW + timedelta(minutes=20) and event.title == "Buy milk"


def test_due_reminder_is_announced_and_can_be_snoozed(rig):
    rig.reply("remind me in 10 minutes to stretch")
    rig.clock.now = NOW + timedelta(minutes=10, seconds=5)
    assert rig.core.reminders.check_now() == 1
    assert wait_for(lambda: rig.rec.of(Events.REMINDER_DUE))  # the app's event bus is asynchronous
    assert rig.rec.of(Events.REMINDER_DUE)[-1]["title"] == "Stretch"
    reply = rig.reply("snooze")
    assert "stretch" in reply.lower() and "in 10 minutes" in reply
    pending = [e for e in reminders(rig) if e.reminded_at is None]
    assert [e.title for e in pending] == ["Stretch"]
    # rounded up to a whole minute: never early, at most a minute late
    assert timedelta(minutes=10) <= pending[0].starts_at - rig.clock.now < timedelta(minutes=11)


def test_cancel_all_asks_first(rig):
    rig.reply("remind me at 5 to call mom")
    rig.reply("remind me tomorrow at 9 am to study")
    assert "Cancel all 2" in rig.reply("cancel all my reminders")
    assert "kept your reminders" in rig.reply("no")
    assert len(reminders(rig)) == 2
    rig.reply("cancel all my reminders")
    assert "2 reminders cancelled" in rig.reply("yes")


def test_remind_is_not_mistaken_for_remember(rig):
    rig.reply("remind me at 6 pm to water the plants")
    assert rig.core.memory.list(rig.session) == []


# -- weather ----------------------------------------------------------------------------------------------------
def test_weather_for_a_named_city(rig):
    reply = rig.say("what's the weather in Pune")
    assert "28 °C" in reply["text"] and "partly cloudy" in reply["text"]
    assert reply["speech"].startswith("It's 28 degrees in Pune")
    assert rig.provider.requests == []  # not sent to web research or the AI


def test_weather_uses_the_saved_home_city_or_asks(rig):
    assert "Which city" in rig.reply("what's the weather like")
    assert "Mumbai" in rig.reply("Mumbai")
    rig.reply("remember that I live in Pune")
    assert "Pune" in rig.reply("how hot is it outside")
    assert "70% chance of rain in Pune tomorrow" in rig.reply("will it rain tomorrow")


def test_weather_failure_is_reported_plainly(paths):
    r = Rig(paths, weather_client=FakeWeatherHTTP(fail=True))
    try:
        assert "couldn't reach the weather service" in r.reply("weather in Pune")
    finally:
        r.close()


# -- translation --------------------------------------------------------------------------------------------------
def test_translation_shows_script_and_speaks_pronunciation(rig):
    rig.provider.answer = json.dumps({"translated_into": "Hindi", "translation": "सुप्रभात", "romanized": "Suprabhaat"})
    reply = rig.say("translate good morning to Hindi")
    assert "**Hindi:** सुप्रभात" in reply["text"] and "Suprabhaat" in reply["text"]
    assert reply["speech"] == "In Hindi: Suprabhaat"
    sent = rig.provider.requests[-1]
    assert "into Hindi" in sent.system_prompt and sent.messages[-1].content == "good morning"


@pytest.mark.parametrize("text,lang,phrase", [
    ("how do you say thank you in Marathi", "Marathi", "thank you"),
    ("what is good night in Spanish", "Spanish", "good night"),
    ("translate to French: where is the station", "French", "where is the station"),
])
def test_translation_phrasings(rig, text, lang, phrase):
    rig.provider.answer = json.dumps({"language": lang, "translation": "x", "romanized": ""})
    rig.reply(text)
    assert f"into {lang}" in rig.provider.requests[-1].system_prompt
    assert rig.provider.requests[-1].messages[-1].content == phrase


def test_weather_question_with_in_is_not_a_translation(rig):
    rig.reply("what is the weather in London")
    assert rig.provider.requests == []


# -- recall ----------------------------------------------------------------------------------------------------------
def test_what_did_we_talk_about_today_summarises_saved_history(rig):
    rig.provider.answer = "Black holes are regions of spacetime."
    rig.reply("tell me about black holes")
    rig.core.everyday_assistant._clock = Clock(datetime.now())  # history timestamps use the real clock
    rig.provider.answer = "- You asked about black holes."
    reply = rig.reply("what did we talk about today")
    assert "You asked about black holes" in reply and "What we talked about today" in reply
    assert "black holes" in rig.provider.requests[-1].messages[-1].content
    assert "didn't talk" in rig.reply("what did we talk about last week")


def test_a_reminder_is_never_early_and_a_bare_one_asks_what_for(rig):
    rig.clock.now = datetime(2026, 10, 22, 11, 21, 52)
    reply = rig.say("remind me after two minutes", source="voice")
    assert "what should I remind you about" in reply["text"] and reply["listen_after"]
    assert reminders(rig) == []
    reply = rig.reply("to call the bank")
    [event] = reminders(rig)
    assert event.title == "Call the bank" and event.starts_at == datetime(2026, 10, 22, 11, 24)   # not 11:23
    assert "in 2 minutes (at 11:24 AM)" in reply


def test_a_generic_reminder_says_what_it_is():
    from services.schedule_service import reminder_speech

    text = reminder_speech({"title": "Reminder", "kind": "event", "starts_at": "2026-10-22T11:24"},
                           datetime(2026, 10, 22, 11, 24, 5))
    assert text.startswith("Here's the reminder you asked for")
