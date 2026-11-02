"""Module 4 security: policy engine, path guard, risk levels, permissions, secrets."""

import json
import threading

import pytest

from automation.models import Risk, Step, StepStatus, TaskStatus
from automation.registry import ExecContext
from automation.safety import PathGuard, SafetyError, check_url, safe_args
from tests.automation_env import make_env


@pytest.fixture
def env(paths, tmp_path):
    e = make_env(paths, tmp_path)
    yield e
    e.close()


def ctx_for(env, session=None):
    s = session or env.session
    return ExecContext(session=s, user=env.core.auth.require(s), services=env.core.automation.services,
                       cancel=threading.Event())


def evaluate(env, capability, args, session=None):
    return env.core.automation.policy.evaluate(session or env.session, Step(capability, args), ctx_for(env, session))


# ------------------------------------------------------------------ authentication / capabilities
def test_unauthenticated_requests_are_refused(env):
    from services.auth_service import Session

    decision = env.core.automation.policy.evaluate(Session("forged-token"), Step("system.status", {}),
                                                   ctx_for(env))
    assert not decision.allowed and "sign in" in decision.reason.lower()
    reply = env.core.automation_assistant.local_reply(None, "open chrome", "typed")
    assert reply.kind == "auth"  # Module 3's sign-in gate answers; nothing runs


def test_unknown_capability_is_refused(env):
    decision = evaluate(env, "shell.run", {"command": "del /s C:\\"})
    assert not decision.allowed and decision.risk is Risk.HIGH
    task = env.run(Step("shell.run", {"command": "format c:"}))
    assert task.status is TaskStatus.FAILED and task.steps[0].status is StepStatus.DENIED


def test_unknown_arguments_and_wrong_types_are_refused(env):
    assert not evaluate(env, "system.status", {"focus": "cpu", "extra": 1}).allowed
    assert not evaluate(env, "system.status", {"focus": "kernel"}).allowed
    assert not evaluate(env, "input.scroll", {"direction": "up", "amount": "lots"}).allowed


def test_permissions_are_enforced_per_capability(env):
    bob = env.second_user()
    # normal users don't get developer commands or automation administration by default
    assert not evaluate(env, "dev.run", {"project": str(env.projects / "demo"), "template": "git_status"},
                        bob).allowed
    assert not evaluate(env, "automation.permit_folder", {"path": str(env.outside)}, bob).allowed
    env.core.auth.set_permission(env.session, env.core.auth.require(bob).id, "automation.files", False)
    decision = evaluate(env, "files.create_folder", {"parent": str(env.sandbox), "name": "x"}, bob)
    assert not decision.allowed and "automation.files" in decision.reason


# ------------------------------------------------------------------ risk
def test_risk_levels_and_confirmation_requirements(env):
    low = evaluate(env, "files.create_folder", {"parent": str(env.sandbox), "name": "New"})
    assert low.allowed and low.risk is Risk.LOW and not low.needs_confirmation
    (env.downloads / "a.txt").write_text("x")
    medium = evaluate(env, "files.move", {"sources": [str(env.downloads / "a.txt")], "destination": str(env.sandbox)})
    assert medium.risk is Risk.MEDIUM and medium.needs_confirmation
    high = evaluate(env, "files.delete", {"paths": [str(env.downloads / "a.txt")]})
    assert high.risk is Risk.HIGH and high.needs_confirmation


def test_configuration_cannot_lower_risk_below_the_floor(env):
    (env.downloads / "a.txt").write_text("x")
    env.core.automation_settings._config.risk_overrides["files.delete"] = "low"
    env.core.automation_settings._config.auto_approve_medium.append("files.delete")
    high = evaluate(env, "files.delete", {"paths": [str(env.downloads / "a.txt")]})
    assert high.risk is Risk.HIGH and high.needs_confirmation


def test_planner_supplied_risk_is_ignored(env):
    """A plan step saying it is 'safe' changes nothing: the policy engine decides."""
    from automation.planner import validate_plan

    (env.downloads / "a.txt").write_text("x")
    steps = validate_plan(env.core.automation.registry, {"steps": [
        {"capability": "files.delete", "args": {"paths": [str(env.downloads / "a.txt")]}, "risk": "low",
         "description": "totally safe cleanup"}]}, 10)
    decision = env.core.automation.policy.evaluate(env.session, steps[0], ctx_for(env))
    assert decision.risk is Risk.HIGH and decision.needs_confirmation


def test_auto_approve_applies_only_to_medium(env):
    (env.downloads / "a.txt").write_text("x")
    env.core.automation_settings._config.auto_approve_medium.append("files.move")
    d = evaluate(env, "files.move", {"sources": [str(env.downloads / "a.txt")], "destination": str(env.sandbox)})
    assert d.risk is Risk.MEDIUM and not d.needs_confirmation


# ------------------------------------------------------------------ paths
def test_path_guard_blocks_outside_protected_and_traversal(env):
    guard = PathGuard(lambda: env.core.automation_settings.config)
    assert guard.check(env.sandbox / "x.txt", write=True)
    with pytest.raises(SafetyError):
        guard.check(env.outside / "secret.txt")
    with pytest.raises(SafetyError):
        guard.check(str(env.sandbox) + "\\..\\outside\\secret.txt")       # traversal resolves outside
    with pytest.raises(SafetyError):
        guard.check(env.core.paths.database)                              # DUMBOO's own data is protected
    with pytest.raises(SafetyError):
        guard.check(env.sandbox / ".env")                                  # credential files
    with pytest.raises(SafetyError):
        guard.check(r"\\server\share\file.txt")                            # network paths
    with pytest.raises(SafetyError):
        guard.check(str(env.sandbox / "a.txt") + ":hidden")                # alternate data streams
    with pytest.raises(SafetyError):
        guard.check(env.sandbox, write=True)                               # the permitted root itself


def test_protected_locations_cannot_be_reached_even_if_permitted(env, monkeypatch):
    """Regression: SafetyError is an OSError and must not be swallowed by the protected-folder check."""
    data = env.core.paths.data_dir
    env.core.automation_settings.set_permitted_directories([
        {"path": str(env.sandbox), "write": True}, {"path": str(data.parent), "write": True}])
    guard = PathGuard(lambda: env.core.automation_settings.config)
    with pytest.raises(SafetyError, match="protected"):
        guard.check(data / "dumboo.db")
    config = env.core.automation_settings.config
    from pathlib import Path
    appdata = Path.home() / "AppData" / "Roaming" / "Something"
    assert config.is_protected(appdata)
    assert not config.is_protected(Path.home() / "AppData" / "Local" / "Temp" / "x")  # Temp is exempt


def test_permitting_a_protected_folder_is_refused(env):
    task = env.run(Step("automation.permit_folder", {"path": str(env.core.paths.data_dir)}))
    assert task.steps[0].status is StepStatus.DENIED and "protected" in task.steps[0].error


def test_urls_are_validated(env):
    assert check_url("github.com") == "https://github.com"
    for bad in ("file:///C:/Windows/win.ini", "javascript:alert(1)", "https://user:pass@example.com"):
        with pytest.raises(SafetyError):
            check_url(bad)
    with pytest.raises(SafetyError):
        check_url("https://bad.example.com", ["example.com"])


# ------------------------------------------------------------------ secrets
def test_secrets_are_never_typed_stored_or_logged(env, caplog):
    win = env.desktop.add_window("notepad.exe", "Untitled - Notepad")
    secret = "my password is Hunter2-Secret"
    task = env.run(Step("input.type_text", {"text": secret, "app": "notepad"}))
    assert task.steps[0].status is StepStatus.DENIED
    assert env.desktop.texts[win.hwnd] == ""
    # even allowed private text is not written to history
    task = env.run(Step("input.type_text", {"text": "Dear team, the meeting moves to 3 pm.", "app": "notepad"}))
    assert task.status is TaskStatus.COMPLETED
    row = env.core.automation_history.get(env.session, task.id)
    stored = json.dumps(row)
    assert "meeting moves" not in stored and "not stored" in stored
    assert secret not in caplog.text and "meeting moves" not in caplog.text
    memories = env.core.memory.list(env.session)
    assert not any("Hunter2" in m["memory_value"] for m in memories)


def test_safe_args_redacts_secret_values():
    out = safe_args("web.fill", {"field": "Email", "value": "a@b.c", "note": "api key sk-abcdefghijklmnopqrstu"},
                    ("value",))
    assert out["value"].startswith("[") and out["note"] == "[redacted]"
