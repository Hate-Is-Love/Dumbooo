"""A headless DUMBOO with fake desktop/browser and a permitted sandbox folder, for automation tests."""

from __future__ import annotations

import os
from pathlib import Path

from automation.models import Step, Task
from tests.test_application import OWNER, make_core


class Env:
    def __init__(self, paths, tmp: Path, **kw) -> None:
        self.sandbox = tmp / "sandbox"
        self.downloads = self.sandbox / "Downloads"
        self.projects = self.sandbox / "Projects"
        for d in (self.downloads, self.projects / "demo" / "tests", self.sandbox / "College"):
            d.mkdir(parents=True, exist_ok=True)
        (self.projects / "demo" / "requirements.txt").write_text("pytest\n")
        (self.projects / "demo" / "README.md").write_text("# Demo\n\n## Installation\n\npip install -r requirements.txt\n")
        (self.projects / "demo" / "main.py").write_text("print('hi')\n")
        self.outside = tmp / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("not for dumboo")
        self.core = make_core(paths, **kw)
        self.core.automation_settings.set_permitted_directories([{"path": str(self.sandbox), "write": True}])
        self.core.start()
        self.desktop = self.core.desktop
        self.browser = self.core.browser
        self.uia = self.core.automation.services.uia

    @property
    def session(self):
        return self.core.accounts.session

    def say(self, text: str, source: str = "typed", session=None):
        return self.core.automation_assistant.local_reply(session or self.session, text, source)

    def run(self, *steps: Step, origin: str = "rules", session=None, timeout: float = 8) -> Task:
        task = self.core.automation.submit(session or self.session, "test", list(steps), origin=origin)
        return self.core.automation.wait(task, timeout)

    def finish(self, task: Task, timeout: float = 8) -> Task:
        import time

        end = time.monotonic() + timeout
        while not task.status.finished and time.monotonic() < end:
            self.core.automation.wait(task, 0.5)
        return task

    def downloads_files(self, *names: str, old: bool = True) -> list[Path]:
        out = []
        for n in names:
            p = self.downloads / n
            p.write_text("data " + n)
            if old:
                os.utime(p, (1_600_000_000, 1_600_000_000))
            out.append(p)
        return out

    def second_user(self):
        auth = self.core.auth
        auth.create_user(self.session, "bob", "Bob", "Kiwi-Lamp-456")
        return auth.login("bob", "Kiwi-Lamp-456")

    def close(self) -> None:
        self.core.shutdown()


def make_env(paths, tmp_path, **kw) -> Env:
    return Env(paths, tmp_path, **kw)


__all__ = ["Env", "make_env", "OWNER"]
