"""Module 4 integration: voice and text share one path, history persists, intents, existing modules intact."""

import pytest

from app.event_bus import Events
from app.state_machine import AssistantState as S
from automation import intents
from automation.models import Step
from tests.ai_fakes import ScriptedProvider
from tests.automation_env import make_env
from tests.fakes import RecordingBus, wait_for
from tests.test_application import OWNER, make_core


@pytest.fixture
def env(paths, tmp_path):
    provider = ScriptedProvider(answer="An ordinary AI answer.")
    e = make_env(paths, tmp_path, ai_provider_factory=lambda _c: provider)
    e.provider = provider
    yield e
    e.close()


def test_voice_and_text_use_the_same_controller(env):
    rec = RecordingBus(env.core.bus)
    for n, source in enumerate(("typed", "voice"), 1):
        env.core.bus.publish(Events.USER_TEXT, {"text": f"create a folder named From {source} in {env.sandbox}",
                                                "source": source})
        assert wait_for(lambda: (env.sandbox / f"From {source}").is_dir(), 10)
        # Wait for the reply before the next message: a new message cancels an answer still being written
        # ("latest message wins"). Sending too early made this test fail about 1 run in 5.
        assert wait_for(lambda: len([p for p in rec.of(Events.RESPONSE_READY) if p.get("local")]) >= n, 10), [
            (name, p.get("seq"), p.get("source"), (p.get("text") or p.get("reason") or "")[:50])
            for name, p in rec.events if name in (Events.RESPONSE_READY, Events.RESPONSE_CANCELLED)]
    tasks = env.core.automation_history.list(env.session)
    assert sorted(t["source"] for t in tasks) == ["typed", "voice"]
    assert env.provider.requests == []  # automation never needed the AI provider
    # normal chat still goes to the AI exactly as before
    env.core.bus.publish(Events.USER_TEXT, {"text": "tell me a fun fact about space", "source": "typed"})
    assert wait_for(lambda: any(p.get("text") == "An ordinary AI answer." for p in rec.of(Events.RESPONSE_READY)), 5)


def test_automation_exchanges_are_saved_in_conversation_history(env):
    env.core.bus.publish(Events.USER_TEXT, {"text": "check my disk space", "source": "typed"})
    assert wait_for(lambda: env.core.automation_history.list(env.session), 10)
    env.core.recorder.flush()
    assert wait_for(lambda: any("check my disk space" in m["content"] for c in
                                env.core.history.list_conversations(env.session)
                                for m in env.core.history.get_conversation(env.session, c["id"])["messages"]), 5)


def test_spoken_confirmation_question_keeps_listening(env):
    (env.downloads / "old.txt").write_text("x")
    rec = RecordingBus(env.core.bus)
    env.core.bus.publish(Events.WAKE_REQUESTED, {"source": "test"})
    assert wait_for(lambda: env.core.state.state == S.IDLE, 3)
    env.core.bus.publish(Events.USER_TEXT, {"text": f"delete {env.downloads / 'old.txt'}", "source": "voice"})
    assert wait_for(lambda: any(p.get("reason") == "waiting for your answer" for p in rec.of(Events.STATE_CHANGED)),
                    10)
    env.core.bus.publish(Events.USER_TEXT, {"text": "yes", "source": "voice"})
    assert wait_for(lambda: not (env.downloads / "old.txt").exists(), 10)


def test_task_history_persists_across_restarts(paths, tmp_path):
    env = make_env(paths, tmp_path)
    task = env.run(Step("files.create_folder", {"parent": str(env.sandbox), "name": "persist"}))
    env.close()
    core = make_core(paths, signed_in=False)
    try:
        core.start()
        core.accounts.login(OWNER[0], OWNER[2])
        rows = core.automation_history.list(core.accounts.session)
        assert [r["id"] for r in rows] == [task.id] and rows[0]["status"] == "completed"
        assert rows[0]["steps"][0]["verification"] == "the folder exists on disk"
    finally:
        core.shutdown()


def test_unfinished_tasks_are_marked_interrupted_after_a_restart(paths, tmp_path):
    env = make_env(paths, tmp_path)
    (env.downloads / "a.txt").write_text("x")
    pending = env.run(Step("files.create_folder", {"parent": str(env.sandbox), "name": "done"}),
                      Step("files.delete", {"paths": [str(env.downloads / "a.txt")]}))
    task_id = pending.id
    env.close()
    import json
    import sqlite3

    conn = sqlite3.connect(paths.database)  # simulate a crash: the row still says "running"
    steps = json.loads(conn.execute("SELECT steps_json FROM automation_tasks WHERE id = ?", (task_id,)).fetchone()[0])
    steps[1]["status"] = "running"
    conn.execute("UPDATE automation_tasks SET status = 'running', error = '', steps_json = ? WHERE id = ?",
                 (json.dumps(steps), task_id))
    conn.commit()
    conn.close()
    core = make_core(paths, signed_in=False)
    try:
        core.start()
        core.accounts.login(OWNER[0], OWNER[2])
        row = core.automation_history.get(core.accounts.session, task_id)
        assert row["status"] in ("partially_completed", "cancelled") and "closed before" in row["error"]
    finally:
        core.shutdown()


def test_signing_out_cancels_the_users_running_automation(env):
    (env.downloads / "a.txt").write_text("x")
    task = env.run(Step("files.delete", {"paths": [str(env.downloads / "a.txt")]}))
    env.core.accounts.logout("signed out")
    env.finish(task)
    assert task.status.value == "cancelled" and (env.downloads / "a.txt").exists()


def test_user_export_contains_automation_history(env):
    env.run(Step("system.status", {"focus": "cpu"}))
    data = env.core.history.export(env.session)
    assert len(data["automation_tasks"]) == 1


def test_service_status_reports_automation(env):
    status = env.core.service_status()["automation"]
    assert status["ok"] and "actions" in status["detail"]


# ------------------------------------------------------------------ intent recognition
@pytest.mark.parametrize("text, capability", [
    ("Hey DUMBOO, open Chrome.", "apps.open"),
    ("Minimize Chrome and switch to Notepad", "apps.window"),
    ("close notepad", "apps.close"),
    ("Find my latest Python project.", "files.latest_project"),
    ("Create a folder named AI Projects.", "files.create_folder"),
    ("Organize my Downloads folder.", "files.organize"),
    ("Save the current document.", "input.press_keys"),
    ("Copy the selected text.", "input.copy_selection"),
    ("Explain the error on my screen.", "screen.explain"),
    ("Find the Save button.", "screen.find"),
    ("Look at the error and suggest a fix.", "screen.explain"),
    ("Check my laptop performance.", "system.status"),
    ("How much RAM is available?", "system.status"),
    ("Why is my laptop slow?", "system.diagnose"),
    ("Which applications are consuming the most memory?", "system.processes"),
    ("Check my disk space.", "system.status"),
    ("Open YouTube.", "web.open"),
    ("Search for official Python documentation.", "web.search"),
    ("take a screenshot", "screen.save_screenshot"),
    ("type hello world into notepad", "input.type_text"),
])
def test_example_commands_are_recognised(env, text, capability):
    env.desktop.add_window("notepad.exe", "Untitled - Notepad")
    ctx = env.core.automation_assistant._ctx(env.session, env.core.auth.require(env.session))
    result = intents.parse(text, ctx)
    assert isinstance(result, intents.Plan), (text, result)
    assert result.steps[0].capability == capability


@pytest.mark.parametrize("text", ["tell me a joke", "find the derivative of x squared", "move on to the next topic",
                                  "what is the capital of France?", "start a story about dragons",
                                  "remember that my favourite colour is blue", "forget my favourite colour",
                                  "write a poem about the sea"])
def test_conversation_is_not_hijacked(env, text):
    ctx = env.core.automation_assistant._ctx(env.session, env.core.auth.require(env.session))
    assert intents.parse(text, ctx) is None


def test_multi_clause_requests_keep_context(env):
    env.core.automation_settings.add_application("testeditor", {
        "name": "Test Editor", "paths": [r"%WINDIR%\System32\notepad.exe"], "process": "notepad.exe",
        "kind": "editor", "opens": ["folder", "file"]})
    env.say(f"remember that my demo project is in {env.projects / 'demo'}")
    env.say("remember my usual coding application is test editor")
    ctx = env.core.automation_assistant._ctx(env.session, env.core.auth.require(env.session))
    plan = intents.parse("Open my project and run its tests", ctx)
    assert [s.capability for s in plan.steps] == ["apps.open", "dev.run"]
    assert plan.steps[1].args["project"] == str(env.projects / "demo")
