"""Module 4 feature 2: smart file and folder management (real files in a temporary sandbox)."""

import pytest

from automation.models import Step, StepStatus, TaskStatus
from tests.automation_env import make_env


@pytest.fixture
def env(paths, tmp_path):
    e = make_env(paths, tmp_path)
    yield e
    e.close()


def test_search_by_name_extension_and_folder(env):
    env.downloads_files("Report 2024.pdf", "report-notes.txt", "photo.jpg")
    task = env.run(Step("files.search", {"query": "report"}))
    rows = task.steps[0].data["results"]
    assert task.status is TaskStatus.COMPLETED and {r["name"] for r in rows} == {"Report 2024.pdf", "report-notes.txt"}
    task = env.run(Step("files.search", {"extension": "pdf", "folder": str(env.downloads)}))
    assert [r["name"] for r in task.steps[0].data["results"]] == ["Report 2024.pdf"]
    task = env.run(Step("files.search", {"query": "nothing-like-this"}))
    assert task.status is TaskStatus.COMPLETED and "No " in task.steps[0].message


def test_search_never_leaves_permitted_folders(env):
    task = env.run(Step("files.search", {"query": "secret"}))
    assert task.steps[0].data["paths"] == []  # the file outside the sandbox is never found
    task = env.run(Step("files.search", {"query": "secret", "folder": str(env.outside)}))
    assert task.steps[0].status is StepStatus.DENIED and "not permitted" in task.steps[0].error


def test_content_search_reads_text_and_docx(env):
    import zipfile

    (env.downloads / "notes.md").write_text("The launch date is 12 March.")
    with zipfile.ZipFile(env.downloads / "plan.docx", "w") as z:
        z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
                                        '2006/main"><w:body><w:p><w:r><w:t>Budget for the launch</w:t></w:r></w:p>'
                                        '</w:body></w:document>')
    task = env.run(Step("files.search_content", {"text": "launch"}))
    assert {p.split("\\")[-1] for p in task.steps[0].data["paths"]} == {"notes.md", "plan.docx"}


def test_create_folder_and_file_are_verified(env):
    task = env.run(Step("files.create_folder", {"parent": str(env.sandbox), "name": "AI Projects"}))
    assert task.status is TaskStatus.COMPLETED and (env.sandbox / "AI Projects").is_dir()
    assert "exists on disk" in task.steps[0].verification
    task = env.run(Step("files.create_file", {"folder": str(env.sandbox / "AI Projects"), "name": "notes.txt",
                                              "content": "hello"}))
    assert (env.sandbox / "AI Projects" / "notes.txt").read_text() == "hello"
    assert task.steps[0].changed == [str(env.sandbox / "AI Projects" / "notes.txt")]


def test_existing_files_are_never_overwritten_silently(env):
    target = env.sandbox / "keep.txt"
    target.write_text("original")
    task = env.run(Step("files.create_file", {"folder": str(env.sandbox), "name": "keep.txt", "content": "new"}))
    assert task.status is TaskStatus.FAILED and target.read_text() == "original"
    # explicitly replacing is high risk, asks first, and keeps a backup
    task = env.run(Step("files.create_file", {"folder": str(env.sandbox), "name": "keep.txt", "content": "new",
                                              "overwrite": True}))
    assert task.status is TaskStatus.AWAITING_CONFIRMATION and task.steps[0].risk.value == "high"
    env.core.automation.confirm(env.session, task.id, True)
    env.finish(task)
    assert target.read_text() == "new"
    backups = list(env.core.automation.services.backup_dir.iterdir())
    assert len(backups) == 1 and backups[0].read_text() == "original"


def test_executable_files_are_never_created_or_opened(env):
    task = env.run(Step("files.create_file", {"folder": str(env.sandbox), "name": "run.bat", "content": "del *"}))
    assert task.steps[0].status is StepStatus.DENIED and not (env.sandbox / "run.bat").exists()
    (env.sandbox / "tool.exe").write_bytes(b"MZ")
    task = env.run(Step("files.open", {"path": str(env.sandbox / "tool.exe")}))
    assert task.steps[0].status is StepStatus.DENIED and env.desktop.opened == []


def test_rename_and_move_need_confirmation_and_are_verified(env):
    a, b = env.downloads_files("a.pdf", "b.pdf")
    task = env.run(Step("files.rename", {"path": str(a), "new_name": "final"}))
    assert task.status is TaskStatus.AWAITING_CONFIRMATION
    env.core.automation.confirm(env.session, task.id, True)
    env.finish(task)
    assert (env.downloads / "final.pdf").exists() and not a.exists()  # extension kept
    task = env.run(Step("files.move", {"sources": [str(env.downloads / "final.pdf"), str(b)],
                                       "destination": str(env.sandbox / "College")}))
    env.core.automation.confirm(env.session, task.id, True)
    env.finish(task)
    assert task.status is TaskStatus.COMPLETED
    assert sorted(p.name for p in (env.sandbox / "College").iterdir()) == ["b.pdf", "final.pdf"]


def test_move_skips_name_conflicts_and_reports_them(env):
    (a,) = env.downloads_files("same.txt")
    (env.sandbox / "College" / "same.txt").write_text("already there")
    task = env.run(Step("files.move", {"sources": [str(a)], "destination": str(env.sandbox / "College")}))
    env.core.automation.confirm(env.session, task.id, True)
    env.finish(task)
    assert task.status is TaskStatus.FAILED and "already exists" in task.steps[0].message
    assert a.exists() and (env.sandbox / "College" / "same.txt").read_text() == "already there"


def test_declining_a_destructive_action_changes_nothing(env):
    (a,) = env.downloads_files("important.docx")
    task = env.run(Step("files.delete", {"paths": [str(a)]}))
    assert task.status is TaskStatus.AWAITING_CONFIRMATION and task.steps[0].risk.value == "high"
    env.core.automation.confirm(env.session, task.id, False)
    env.finish(task)
    assert task.status is TaskStatus.CANCELLED and a.exists() and env.desktop.recycled == []


def test_delete_goes_to_the_recycle_bin(env):
    (a,) = env.downloads_files("old.txt")
    task = env.run(Step("files.delete", {"paths": [str(a)]}))
    env.core.automation.confirm(env.session, task.id, True)
    env.finish(task)
    assert task.status is TaskStatus.COMPLETED and not a.exists() and env.desktop.recycled == [str(a)]


def test_organize_previews_then_applies_and_can_be_undone(env):
    env.downloads_files("a.pdf", "b.jpg", "c.mp3", "d.zip", "e.unknownext")
    env.downloads_files("fresh.pdf", old=False)            # just downloaded: left alone
    (env.downloads / "partial.crdownload").write_text("x")  # still downloading: left alone
    reply = env.say(f"organize {env.downloads}")
    assert "MEDIUM risk" in reply.text and "PDFs/ (1)" in reply.text
    assert (env.downloads / "a.pdf").exists()  # nothing moved before the answer
    reply = env.say("yes")
    assert "moved 4 of 4" in reply.text
    assert (env.downloads / "PDFs" / "a.pdf").exists() and (env.downloads / "Images" / "b.jpg").exists()
    assert (env.downloads / "fresh.pdf").exists() and (env.downloads / "e.unknownext").exists()
    assert "MEDIUM risk" in env.say("undo the last file operation").text
    assert "Undid 4" in env.say("yes").text
    assert (env.downloads / "a.pdf").exists() and not (env.downloads / "PDFs" / "a.pdf").exists()


def test_duplicates_are_reported_not_deleted(env):
    (env.downloads / "x1.bin").write_bytes(b"same" * 600)
    (env.sandbox / "College" / "x2.bin").write_bytes(b"same" * 600)
    (env.downloads / "y.bin").write_bytes(b"diff" * 600)
    task = env.run(Step("files.duplicates", {}))
    groups = task.steps[0].data["groups"]
    assert len(groups) == 1 and len(groups[0]["paths"]) == 2
    assert (env.downloads / "x1.bin").exists() and (env.sandbox / "College" / "x2.bin").exists()


def test_latest_project_and_recent_documents(env):
    task = env.run(Step("files.latest_project", {"language": "python"}))
    assert task.steps[0].data["path"] == str(env.projects / "demo")
    env.downloads_files("letter.docx", old=False)
    task = env.run(Step("files.recent", {"kind": "documents"}))
    assert task.steps[0].data["paths"][0].endswith("letter.docx")


def test_edit_shows_a_diff_and_keeps_a_backup(env):
    f = env.sandbox / "todo.txt"
    f.write_text("buy milk\n")
    task = env.run(Step("files.write", {"path": str(f), "content": "buy bread\n"}))
    assert task.status is TaskStatus.AWAITING_CONFIRMATION
    assert "-buy milk" in task.steps[0].confirm_details and "+buy bread" in task.steps[0].confirm_details
    env.core.automation.confirm(env.session, task.id, True)
    env.finish(task)
    assert f.read_text() == "buy bread\n"
    assert task.undo and task.undo[0]["op"] == "restore"


def test_python_files_are_syntax_checked(env):
    task = env.run(Step("files.create_file", {"folder": str(env.sandbox), "name": "bad.py", "content": "def f(:\n"}))
    env.core.automation.confirm(env.session, task.id, True)  # code is previewed (medium risk)
    env.finish(task)
    assert "syntax error" in task.steps[0].verification


def test_summary_uses_the_document_text(paths, tmp_path):
    from tests.ai_fakes import ScriptedProvider

    provider = ScriptedProvider(answer="Sales grew by 20% in Q3.")
    env = make_env(paths, tmp_path, ai_provider_factory=lambda _c: provider)
    try:
        (env.sandbox / "report.txt").write_text("Sales grew 20% in Q3. Costs fell.")
        task = env.run(Step("files.summarize", {"path": str(env.sandbox / "report.txt")}))
        assert task.status is TaskStatus.COMPLETED and "Sales grew by 20%" in task.steps[0].message
        sent = provider.requests[-1].messages[-1].content
        assert "Costs fell" in sent and "<document>" in sent  # the real text, marked as data
    finally:
        env.close()


def test_controlled_folder_access_is_explained(env, monkeypatch):
    """Windows' ransomware protection reports a blocked write as 'No such file'; DUMBOO explains it."""
    import builtins

    real_open = builtins.open

    def blocked(path, mode="r", *a, **k):
        if "w" in mode and str(path).endswith("blocked.txt"):
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", blocked)
    task = env.run(Step("files.create_file", {"folder": str(env.sandbox), "name": "blocked.txt", "content": "x"}))
    assert task.status is TaskStatus.FAILED and "Controlled folder access" in task.steps[0].error
