"""Module 4 feature 8: memory-aware personalisation, scoped to the signed-in user."""

import shutil

import pytest

from automation.models import Step, TaskStatus
from tests.automation_env import make_env


@pytest.fixture
def env(paths, tmp_path):
    e = make_env(paths, tmp_path)
    yield e
    e.close()


def memories(env, session=None):
    return {m["memory_key"]: m["memory_value"] for m in env.core.memory.list(session or env.session)}


def test_saved_project_path_is_retrieved_verified_and_used(env):
    project = env.projects / "demo"
    reply = env.say(f"remember that my DUMBOO project is in {project}")
    assert "I'll remember" in reply.text and memories(env)["dumboo project path"] == str(project)
    env.core.automation_settings.add_application("testeditor", {
        "name": "Test Editor", "paths": [r"%WINDIR%\System32\notepad.exe"], "process": "notepad.exe",
        "kind": "editor", "opens": ["folder", "file"]})
    env.say("remember my usual coding application is test editor")
    reply = env.say("open DUMBOO")
    assert "Opened Test Editor with demo" in reply.text
    assert env.desktop.launched[-1][1] == [str(project)]


def test_this_folder_comes_from_the_open_explorer_window(env):
    project = env.projects / "demo"
    win = env.desktop.add_window("explorer.exe", "demo")
    env.desktop.explorer[win.hwnd] = str(project)
    env.say("remember that my demo project is in this folder")
    assert memories(env)["demo project path"] == str(project)


def test_missing_or_outdated_paths_are_not_used(env):
    project = env.projects / "old"
    project.mkdir()
    env.say(f"remember that my old project is in {project}")
    shutil.rmtree(project)
    env.say("remember my usual coding application is notepad")
    reply = env.say("open my old project")
    assert "no longer exists" in reply.text and env.desktop.launched == []


def test_nothing_is_invented_when_no_memory_exists(env):
    reply = env.say("open my project")
    assert "don't have any project folders saved" in reply.text
    reply = env.say("open my usual coding application")
    assert "don't know your usual coding application" in reply.text
    reply = env.say("open the website I saved for my project")
    assert "don't have a website saved" in reply.text
    assert env.desktop.launched == [] and env.desktop.opened == []


def test_multiple_matches_ask_instead_of_guessing(env):
    for name in ("alpha", "beta"):
        (env.projects / name).mkdir()
        env.say(f"remember that my {name} project is in {env.projects / name}")
    reply = env.say("open my project")
    assert "several saved projects" in reply.text and "alpha" in reply.text and "beta" in reply.text


def test_saved_website_and_folder_are_used(env):
    env.say("remember that the project website is https://example.org/docs")
    reply = env.say("open the website I saved for my project")
    assert "Opened" in reply.text and env.desktop.opened[-1] == "https://example.org/docs"
    env.say(f"remember that my college folder is {env.sandbox / 'College'}")
    (env.downloads / "a.pdf").write_text("x")
    env.say(f"find pdfs in {env.downloads}")
    reply = env.say("move these pdfs into my college folder")
    assert "MEDIUM risk" in reply.text and str(env.sandbox / "College") in reply.text


def test_memory_is_scoped_to_the_signed_in_user(env):
    project = env.projects / "demo"
    env.say(f"remember that my secretproj project is in {project}")
    bob = env.second_user()
    reply = env.say("open my secretproj project", session=bob)
    assert "don't have a folder saved" in reply.text
    assert "secretproj project path" not in memories(env, bob)


def test_task_history_is_private_per_user(env):
    task = env.run(Step("files.create_folder", {"parent": str(env.sandbox), "name": "mine"}))
    bob = env.second_user()
    assert env.core.automation_history.list(bob) == []
    from services.automation_history import TaskNotFound
    with pytest.raises(TaskNotFound):
        env.core.automation_history.get(bob, task.id)
    with pytest.raises(TaskNotFound):
        env.core.automation_history.delete(bob, task.id)
    assert env.core.automation_history.get(env.session, task.id)["status"] == "completed"
    # bob cannot answer the owner's confirmation question either
    (env.downloads / "x.txt").write_text("x")
    pending = env.run(Step("files.delete", {"paths": [str(env.downloads / "x.txt")]}))
    assert env.core.automation.confirm(bob, pending.id, True) is None
    assert (env.downloads / "x.txt").exists()
    env.core.automation.confirm(env.session, pending.id, False)


def test_ordinary_tasks_do_not_write_memories(env):
    before = memories(env)
    env.say("check my disk space")
    env.say(f"create a folder named Temp1 in {env.sandbox}")
    env.say("how much ram is available")
    assert memories(env) == before


def test_workflows_are_saved_and_replayed_with_approval(env):
    env.say(f"create a folder named Reports in {env.sandbox}")
    reply = env.say("remember this workflow as morning setup")
    assert "Saved “morning setup”" in reply.text
    assert memories(env)["workflow morning setup"].startswith("task #")
    shutil.rmtree(env.sandbox / "Reports")
    reply = env.say("run my morning setup workflow")
    assert "Here's my plan" in reply.text  # replayed steps are shown first, then checked again
    reply = env.say("yes")
    assert (env.sandbox / "Reports").is_dir()


def test_continue_the_unfinished_task(env):
    (env.downloads / "a.txt").write_text("x")
    task = env.run(Step("files.create_folder", {"parent": str(env.sandbox), "name": "Step1"}),
                   Step("files.delete", {"paths": [str(env.downloads / "a.txt")]}))
    env.core.automation.confirm(env.session, task.id, False)
    env.finish(task)
    assert task.status is TaskStatus.PARTIALLY_COMPLETED
    reply = env.say("continue the task from yesterday")
    assert "Completed then" in reply.text and "check again" in reply.text
    assert "Here's my plan" in reply.text and "Delete 1 item" in reply.text


def test_automation_memories_can_be_corrected_and_deleted(env):
    env.say(f"remember that my demo project is in {env.projects / 'demo'}")
    (env.projects / "demo2").mkdir()
    env.say(f"remember that my demo project is in {env.projects / 'demo2'}")
    assert memories(env)["demo project path"] == str(env.projects / "demo2")   # corrected, not duplicated
    memory_id = next(m["id"] for m in env.core.memory.list(env.session) if m["memory_key"] == "demo project path")
    env.core.memory.forget(env.session, memory_id)
    assert "don't have a folder saved" in env.say("open my demo project").text


def test_secrets_are_never_stored_as_automation_memories(env):
    reply = env.say("remember that the bank website is https://user:hunter2@bank.example.com")
    assert "user names or passwords" in reply.text
    assert not any("hunter2" in v for v in memories(env).values())
