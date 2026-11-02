"""Module 4 feature 7 and 10: multi-step execution, failures, cancellation, timeouts, emergency stop, planner."""

import json
import time

import pytest

from automation.models import ActionResult, Risk, Step, StepStatus, TaskStatus
from automation.registry import Capability, Param
from tests.ai_fakes import ScriptedProvider
from tests.automation_env import make_env


@pytest.fixture
def env(paths, tmp_path):
    e = make_env(paths, tmp_path)
    yield e
    e.close()


def add_slow(env, name="test.slow", timeout_s=None):
    def handler(ctx, args):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if ctx.cancel.wait(0.05):
                return ActionResult.fail("stopped part-way")
        return ActionResult.done("slow done", "waited")
    env.core.automation.registry.add(Capability(name, "test", {}, handler, "automation.basic", Risk.LOW,
                                                timeout_s=timeout_s))


def folder_step(env, name):
    return Step("files.create_folder", {"parent": str(env.sandbox), "name": name})


def test_multi_step_task_runs_in_order_and_is_verified(env):
    task = env.run(folder_step(env, "one"), folder_step(env, "two"),
                   Step("files.create_file", {"folder": str(env.sandbox / "two"), "name": "a.txt", "content": "x"}))
    assert task.status is TaskStatus.COMPLETED
    assert [s.status for s in task.steps] == [StepStatus.COMPLETED] * 3
    assert (env.sandbox / "two" / "a.txt").read_text() == "x"


def test_failure_in_the_middle_stops_and_reports_partial_completion(env):
    task = env.run(folder_step(env, "first"),
                   Step("files.rename", {"path": str(env.sandbox / "missing.txt"), "new_name": "x"}),
                   folder_step(env, "never"))
    assert task.status is TaskStatus.PARTIALLY_COMPLETED
    assert [s.status for s in task.steps] == [StepStatus.COMPLETED, StepStatus.DENIED, StepStatus.SKIPPED]
    assert (env.sandbox / "first").is_dir() and not (env.sandbox / "never").exists()
    assert "✓" in task.summary and "✗" in task.summary and "Not run" in task.summary


def test_step_timeout_is_enforced(env):
    add_slow(env, timeout_s=0.5)
    started = time.monotonic()
    task = env.finish(env.run(Step("test.slow", {}), folder_step(env, "after")), 10)
    assert time.monotonic() - started < 8
    assert task.status is TaskStatus.FAILED and "Timed out" in task.steps[0].error
    assert not (env.sandbox / "after").exists()


def test_emergency_stop_prevents_later_steps_and_reports_honestly(env):
    add_slow(env)
    task = env.run(folder_step(env, "done-before"), Step("test.slow", {}), folder_step(env, "never"), timeout=1)
    assert task.status is TaskStatus.RUNNING
    report = env.core.automation.emergency_stop("test")
    env.finish(task)
    assert task.status is TaskStatus.PARTIALLY_COMPLETED
    assert [s.status for s in task.steps] == [StepStatus.COMPLETED, StepStatus.CANCELLED, StepStatus.CANCELLED]
    assert (env.sandbox / "done-before").is_dir() and not (env.sandbox / "never").exists()
    assert any("done-before" in c for c in report["completed"]) and len(report["cancelled"]) == 2


def test_voice_stop_command_and_queued_tasks(env):
    add_slow(env)
    first = env.core.automation.submit(env.session, "slow", [Step("test.slow", {})])
    second = env.core.automation.submit(env.session, "queued", [folder_step(env, "queued")])
    env.core.automation.wait(first, 1)
    reply = env.say("stop everything", "voice")
    assert "Emergency stop: 2 task(s)" in reply.text
    env.finish(first)
    env.finish(second)
    assert second.status is TaskStatus.CANCELLED and not (env.sandbox / "queued").exists()


def test_stop_button_cancels_running_automation(env):
    from app.event_bus import Events

    add_slow(env)
    task = env.run(Step("test.slow", {}), timeout=0.5)
    env.core.bus.publish(Events.STOP_SPEAKING, {"source": "ui"})
    env.finish(task)
    assert task.status is TaskStatus.CANCELLED


def test_running_command_is_killed_on_cancel(env, tmp_path):
    import sys
    import threading

    from automation.platform.processes import ProcessRunner

    runner = ProcessRunner()
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    started = time.monotonic()
    result = runner.run([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, 60, cancel)
    assert result.cancelled and time.monotonic() - started < 10 and not result.ok
    result = runner.run([sys.executable, "-c", "print('out'); raise SystemExit(3)"], tmp_path, 60, threading.Event())
    assert result.exit_code == 3 and "out" in result.output and not result.ok


def test_confirmation_timeout_cancels_the_step(env):
    env.core.automation_settings._config.limits["confirmation_timeout_s"] = 1
    (env.downloads / "a.txt").write_text("x")
    task = env.finish(env.run(Step("files.delete", {"paths": [str(env.downloads / "a.txt")]})), 10)
    assert task.status is TaskStatus.CANCELLED and task.confirmation == "expired"
    assert (env.downloads / "a.txt").exists()


def test_argument_references_between_steps(env):
    (env.downloads / "report.pdf").write_text("x")
    task = env.run(Step("files.search", {"query": "report", "extension": "pdf"}),
                   Step("files.copy", {"source": "{{step1.path}}", "destination": str(env.sandbox / "College")}))
    assert task.status is TaskStatus.COMPLETED and (env.sandbox / "College" / "report.pdf").exists()


# ------------------------------------------------------------------ the AI planner
def plan_env(paths, tmp_path, answer):
    provider = ScriptedProvider(answer=answer)
    return make_env(paths, tmp_path, ai_provider_factory=lambda _c: provider), provider


def test_planner_never_executes_arbitrary_commands(paths, tmp_path):
    answer = json.dumps({"type": "plan", "title": "x", "steps": [
        {"capability": "shell.run", "args": {"command": "powershell -c Remove-Item -Recurse C:\\"}}]})
    env, provider = plan_env(paths, tmp_path, answer)
    try:
        reply = env.say("please back up my college folder into my documents folder")
        assert "couldn't turn that into a safe plan" in reply.text
        assert len(provider.requests) == 2  # one retry with the rejection reason, then refusal
        assert "not an available capability" in provider.requests[1].messages[-1].content
        assert env.core.automation_history.list(env.session) == []  # no task was even created
    finally:
        env.close()


def test_planner_plan_is_validated_and_approved_before_anything_runs(paths, tmp_path):
    env, provider = None, None
    answer = json.dumps({"type": "plan", "title": "Back up College", "steps": [
        {"capability": "files.create_folder", "args": {"parent": "SANDBOX", "name": "Backup"},
         "description": "Create a Backup folder"},
        {"capability": "files.copy", "args": {"source": "SANDBOX\\College", "destination": "SANDBOX\\Backup"},
         "description": "Copy College into it", "risk": "low"}]})
    tmp_sandbox = str(tmp_path / "sandbox").replace("\\", "\\\\")
    env, provider = plan_env(paths, tmp_path, answer.replace("SANDBOX", tmp_sandbox))
    try:
        (env.sandbox / "College" / "notes.txt").write_text("n")
        reply = env.say("please back up my college folder into a new backup folder")
        assert "Here's my plan" in reply.text and "Shall I go ahead" in reply.text
        assert not (env.sandbox / "Backup").exists()  # nothing ran before approval
        reply = env.say("yes")
        assert "Done" in reply.text
        assert (env.sandbox / "Backup" / "College" / "notes.txt").exists()
        system = provider.requests[0].system_prompt
        assert "files.copy(" in system and "dev.run(" in system  # capability list, owner permissions
    finally:
        env.close()


def test_planner_clarifies_and_chat_is_left_alone(paths, tmp_path):
    env, provider = plan_env(paths, tmp_path, json.dumps({"type": "clarify", "question": "Which folder?"}))
    try:
        assert env.say("please archive my project files").text == "Which folder?"
        provider.answer = "Hello!"  # ordinary chat never reaches the planner
        assert env.say("how are you today?") is None or env.say("how are you today?").kind != "automation"
    finally:
        env.close()


def test_plan_with_a_denied_step_does_not_start(paths, tmp_path):
    answer = json.dumps({"type": "plan", "title": "x", "steps": [
        {"capability": "files.create_folder", "args": {"parent": "C:\\Windows", "name": "x"}}]})
    env, _ = plan_env(paths, tmp_path, answer)
    try:
        reply = env.say("please make a scratch folder named x in the system folder")
        assert "refused" in reply.text.lower() or "protected" in reply.text.lower() or "not permitted" in reply.text
        assert not (env.sandbox / "x").exists()
    finally:
        env.close()
