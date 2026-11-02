"""Casual, slang and Hindi-mixed commands reach the right action; ordinary chat is not hijacked."""
import pytest

from tests.test_automation_integration import env  # noqa: F401 (fixture)

from automation import intents

CASES = [
    # (text, expected capability)
    ("open youtube", "web.open"), ("open yt", "web.open"), ("pull up youtube", "web.open"),
    ("put on youtube", "web.open"), ("fire up youtube", "web.open"), ("bring up youtube", "web.open"),
    ("launch youtube", "web.open"), ("youtube", "web.open"), ("youtube kholo", "web.open"),
    ("can u open youtube pls", "web.open"), ("yo open youtube real quick", "web.open"),
    ("gimme youtube", "web.open"), ("hop on youtube", "web.open"), ("go to youtube", "web.open"),
    ("take me to youtube", "web.open"), ("open insta", "web.open"), ("open ig", "web.open"),
    ("open fb", "web.open"), ("open gmail", "web.open"), ("open whatsapp", "web.open"),
    ("open netflix", "web.open"), ("open spotify", "web.open"), ("open chatgpt", "web.open"),
    ("open google", "web.open"), ("open twitter", "web.open"), ("open x", "web.open"),
    ("play despacito on youtube", "web.open"), ("search youtube for lofi music", "web.open"),
    ("fire up notepad", "apps.open"), ("open up notepad", "apps.open"), ("pull up notepad", "apps.open"),
    ("start notepad", "apps.open"), ("open the calculator", "apps.open"), ("open calc", "apps.open"),
    ("kill notepad", "apps.force_close"), ("shut notepad", "apps.close"), ("close notepad", "apps.close"),
    ("minimise notepad", "apps.window"), ("hide notepad", "apps.window"), ("notepad band karo", "apps.close"),
    ("whats my battery at", "system.status"), ("hows my ram", "system.status"),
    ("screenshot this", "screen.save_screenshot"), ("snap the screen", "screen.save_screenshot"),
    ("google best laptops under 50k", "web.search"), ("look up python tutorials", "web.search"),
]


@pytest.mark.parametrize("text,cap", CASES)
def test_slang_commands_are_understood(env, text, cap):
    env.desktop.add_window("notepad.exe", "Untitled - Notepad")
    ctx = env.core.automation_assistant._ctx(env.session, env.core.auth.require(env.session))
    result = intents.parse(text, ctx)
    got = result.steps[0].capability if isinstance(result, intents.Plan) else type(result).__name__
    assert got == cap, f"{text!r} -> {got} {getattr(result, 'text', '')[:60]}"


def test_media_requests_open_the_search_results(env):
    ctx = env.core.automation_assistant._ctx(env.session, env.core.auth.require(env.session))
    plan = intents.parse("yo play lofi hip hop on yt", ctx)
    assert plan.steps[0].args["url"] == "https://www.youtube.com/results?search_query=lofi+hip+hop"


@pytest.mark.parametrize("text", ["show me a joke", "i want to learn python", "play a game with me", "go to sleep",
                                  "start a story about a dragon", "can you explain recursion", "open your heart",
                                  "bring up the topic of climate change", "what's the meaning of life"])
def test_casual_chat_is_not_hijacked(env, text):
    ctx = env.core.automation_assistant._ctx(env.session, env.core.auth.require(env.session))
    assert not isinstance(intents.parse(text, ctx), intents.Plan), text
