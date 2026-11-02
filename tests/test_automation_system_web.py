"""Module 4 features 5, 6 and 9: system monitoring, browser/web automation, developer automation."""

import sys
from types import SimpleNamespace

import pytest

from automation.models import Step, StepStatus, TaskStatus
from services.resource_alerts import ResourceAlerts
from services.system_monitor import SystemMonitor
from tests.automation_env import make_env
from tests.fakes import RecordingBus


@pytest.fixture
def env(paths, tmp_path):
    e = make_env(paths, tmp_path)
    yield e
    e.close()


class FakePsutil:
    """Deterministic psutil stand-in."""

    def __init__(self, cpu=12.0, ram=40.0, disk_percent=50.0, battery=80.0, plugged=True):
        self.cpu, self.ram, self.disk_percent, self.battery, self.plugged = cpu, ram, disk_percent, battery, plugged

    def cpu_percent(self, interval=None):
        return self.cpu

    def cpu_count(self, logical=True):
        return 8 if logical else 4

    def virtual_memory(self):
        total = 16 * 1024**3
        return SimpleNamespace(percent=self.ram, used=total * self.ram / 100, total=total)

    def sensors_battery(self):
        return None if self.battery is None else SimpleNamespace(percent=self.battery, power_plugged=self.plugged)

    def disk_usage(self, path):
        total = 500 * 1024**3
        used = total * self.disk_percent / 100
        return SimpleNamespace(total=total, used=used, free=total - used, percent=self.disk_percent)

    def disk_partitions(self, all=False):
        return [SimpleNamespace(mountpoint="C:\\", fstype="NTFS", opts="rw,fixed")]

    def process_iter(self, attrs=None):
        procs = []
        for pid, name, mb, cpu in ((10, "chrome.exe", 900, 30.0), (11, "chrome.exe", 300, 10.0),
                                   (12, "Code.exe", 500, 5.0), (13, "svchost.exe", 200, 1.0)):
            p = SimpleNamespace(pid=pid, info={"pid": pid, "name": name,
                                               "memory_info": SimpleNamespace(rss=mb * 1024**2)})
            p.cpu_percent = (lambda c: (lambda interval=None: c * 8))(cpu)
            procs.append(p)
        return procs

    def net_if_stats(self):
        return {"Wi-Fi": SimpleNamespace(isup=True)}

    def boot_time(self):
        return 1_700_000_000


# ------------------------------------------------------------------ system monitoring
def test_system_metrics_are_measured_not_invented(env):
    env.core.monitor._ps = FakePsutil(cpu=12, ram=40)
    task = env.run(Step("system.status", {"focus": "overview"}))
    text = task.steps[0].message
    assert "CPU load: 12%" in text and "40% used" in text and "9.6 GB available" in text
    assert "Battery: 80% (charging)" in text and "250.0 GB free of 500.0 GB" in text
    env.core.monitor._ps = FakePsutil(battery=None)
    task = env.run(Step("system.status", {"focus": "battery"}))
    assert "none detected" in task.steps[0].message  # never a made-up 100%


def test_top_processes_group_programs_and_separate_windows_itself(env):
    env.core.monitor._ps = FakePsutil()
    task = env.run(Step("system.processes", {"by": "memory"}))
    text = task.steps[0].message
    assert "chrome.exe (2 processes): 1200 MB" in text and text.index("chrome.exe") < text.index("Code.exe")
    assert "Windows itself: svchost.exe" in text


def test_slow_laptop_diagnosis_uses_live_numbers_and_saved_notes(env):
    env.core.monitor._ps = FakePsutil(cpu=95, ram=93)
    env.core.memory.remember(env.session, "laptop ram", "16 GB", "fact")
    task = env.run(Step("system.diagnose", {}))
    text = task.steps[0].message
    assert "processor is busy: 95%" in text and "Memory is nearly full: 93%" in text
    assert "From your saved notes (not measured now)" in text and "16 GB" in text
    assert "haven't closed or changed anything" in text


def test_resource_alerts_need_sustained_load_and_have_a_cooldown():
    clock = [0.0]
    ps = FakePsutil(cpu=97, ram=40)
    bus = __import__("app.event_bus", fromlist=["EventBus"]).EventBus(synchronous=True)
    rec = RecordingBus(bus)
    monitor = SystemMonitor(ps=ps)
    alerts = ResourceAlerts(bus, monitor, lambda: {"sustain_s": 60, "cpu_alert_percent": 90, "ram_alert_percent": 90,
                                                   "disk_alert_free_percent": 10, "battery_alert_percent": 15},
                            clock=lambda: clock[0])
    assert alerts.check() == []           # a spike is not an alert
    clock[0] = 61
    assert [a["metric"] for a in alerts.check()] == ["cpu"]
    clock[0] = 120
    assert alerts.check() == []           # cooldown
    ps.cpu, ps.disk_percent = 10, 95
    assert [a["metric"] for a in alerts.check()] == ["disk"]
    assert len(rec.of("system.alert")) == 2


def test_monitoring_survives_unavailable_sensors(env):
    class Broken(FakePsutil):
        def virtual_memory(self):
            raise OSError("no access")

    env.core.monitor._ps = Broken()
    task = env.run(Step("system.status", {"focus": "memory"}))
    assert task.status is TaskStatus.COMPLETED and "Memory: unavailable" in task.steps[0].message


# ------------------------------------------------------------------ web
def test_open_website_in_the_users_browser_is_verified(env):
    task = env.run(Step("web.open", {"url": "youtube"}))
    assert task.status is TaskStatus.COMPLETED and env.desktop.opened[-1] == "https://www.youtube.com"
    assert "Youtube - Google Chrome" in task.steps[0].verification


def test_unreachable_site_is_not_opened(env):
    env.core.automation.services.probe = lambda url: (False, "the site could not be reached")
    task = env.run(Step("web.open", {"url": "https://no-such-site.example"}))
    assert task.status is TaskStatus.FAILED and env.desktop.opened == []


def test_dumboo_browser_navigation_success_and_failure(env):
    task = env.run(Step("web.open", {"url": "https://docs.python.org/3/", "browser": "dumboo"}))
    assert task.status is TaskStatus.COMPLETED and "HTTP 200" in task.steps[0].verification
    env.browser.failing["https://down.example/"] = "the site name could not be found"
    task = env.run(Step("web.open", {"url": "https://down.example/", "browser": "dumboo"}))
    assert task.status is TaskStatus.FAILED and "could not be found" in task.steps[0].error


def test_search_results_tabs_and_open_result(env):
    reply = env.say("search for official python documentation")
    assert "Welcome to Python.org" in reply.text
    reply = env.say("open the second result")
    assert "Opened result 2" in reply.text
    task = env.run(Step("web.tabs", {"action": "list"}))
    assert task.steps[0].data["tabs"]


def test_download_is_verified_and_never_opened(env):
    url = "https://example.org/files/guide.pdf"
    env.browser.files[url] = b"%PDF-1.4 test"
    reply = env.say(f"download {url} into {env.downloads} folder")
    target = env.downloads / "guide.pdf"
    assert target.read_bytes() == b"%PDF-1.4 test" and "haven't opened it" in reply.text
    assert env.desktop.opened == []
    task = env.run(Step("web.download", {"url": "https://example.org/setup.exe", "folder": str(env.downloads)}))
    assert task.steps[0].status is StepStatus.DENIED  # programs are not downloaded by default
    task = env.run(Step("web.download", {"url": "https://example.org/missing.pdf", "folder": str(env.downloads)}))
    assert task.status is TaskStatus.FAILED and "404" in task.steps[0].error


def test_forms_never_get_passwords_and_submitting_asks(env):
    env.run(Step("web.open", {"url": "https://example.org/signup", "browser": "dumboo"}))
    task = env.run(Step("web.fill", {"field": "Password", "value": "Secret123!"}))
    assert task.steps[0].status is StepStatus.DENIED and env.browser.filled == {}
    task = env.run(Step("web.fill", {"field": "Email", "value": "me@example.org"}))
    assert task.status is TaskStatus.AWAITING_CONFIRMATION
    env.core.automation.confirm(env.session, task.id, True)
    env.finish(task)
    assert env.browser.filled == {"Email": "me@example.org"}
    task = env.run(Step("web.click", {"text": "Submit order"}))
    assert task.steps[0].risk.value == "high"
    env.core.automation.confirm(env.session, task.id, False)
    env.finish(task)
    assert env.browser.clicked == []


# ------------------------------------------------------------------ developer automation
def make_venv_project(tmp_path):
    """A project whose '.venv' python is the test interpreter (so commands really run)."""
    project = tmp_path / "sandbox" / "Projects" / "real"
    (project / ".venv" / "Scripts").mkdir(parents=True)
    import os
    os.link(sys.executable, project / ".venv" / "Scripts" / "python.exe") if False else None
    return project


def test_run_tests_really_runs_and_reports_results(env, tmp_path, monkeypatch):
    from automation.actions import dev

    project = env.projects / "demo"
    (project / "tests" / "test_ok.py").write_text("def test_one():\n    assert 1 + 1 == 2\n\n"
                                                  "def test_two():\n    assert 'a' in 'abc'\n")
    monkeypatch.setattr(dev, "venv_python", lambda p: __import__("pathlib").Path(sys.executable))
    task = env.finish(env.run(Step("dev.run", {"project": str(project), "template": "tests"})), 120)
    assert task.status is TaskStatus.COMPLETED, task.steps[0].error
    assert "2 passed" in task.steps[0].message and "tests really ran" in task.steps[0].verification
    (project / "tests" / "test_bad.py").write_text("def test_bad():\n    assert 2 + 2 == 5\n")
    task = env.finish(env.run(Step("dev.run", {"project": str(project), "template": "tests"})), 120)
    assert "1 failed" in task.steps[0].message and "FAILED" in task.steps[0].message
    assert task.steps[0].data["exit_code"] == 1


def test_installs_ask_first_and_arbitrary_templates_are_refused(env, monkeypatch):
    from automation.actions import dev

    project = env.projects / "demo"
    monkeypatch.setattr(dev, "venv_python", lambda p: __import__("pathlib").Path(sys.executable))
    task = env.run(Step("dev.run", {"project": str(project), "template": "install_requirements"}))
    assert task.status is TaskStatus.AWAITING_CONFIRMATION
    assert "pip install -r requirements.txt" in task.steps[0].confirm_details
    env.core.automation.confirm(env.session, task.id, False)
    env.finish(task)
    task = env.run(Step("dev.run", {"project": str(project), "template": "rm -rf /"}))
    assert task.steps[0].status is StepStatus.DENIED


def test_project_without_venv_is_reported_not_guessed(env):
    reply = env.say(f"remember that my demo project is in {env.projects / 'demo'}")
    reply = env.say("run the tests for my demo project")
    assert "no virtual environment" in reply.text


def test_prepare_project_workflow(env):
    env.core.automation_settings.add_application("testeditor", {
        "name": "Test Editor", "paths": [r"%WINDIR%\System32\notepad.exe"], "process": "notepad.exe",
        "kind": "editor", "opens": ["folder", "file"]})
    env.say(f"remember that my demo project is in {env.projects / 'demo'}")
    env.say("remember my usual coding application is test editor")
    reply = env.say("prepare my demo project for development")
    assert "Done: Prepare demo for development" in reply.text
    assert "Installation" in reply.text and "no virtual environment" in reply.text


def test_generated_code_is_previewed_then_created_and_checked(paths, tmp_path):
    from tests.ai_fakes import ScriptedProvider

    code = "def factorial(n):\n    return 1 if n <= 1 else n * factorial(n - 1)\n"
    env = make_env(paths, tmp_path, ai_provider_factory=lambda _c: ScriptedProvider(answer="```python\n" + code + "```"))
    try:
        reply = env.say(f"create a python file that calculates factorial in {env.sandbox}")
        assert "def factorial" in reply.text and "Shall I do it" in reply.text
        assert not (env.sandbox / "factorial.py").exists()
        reply = env.say("yes")
        assert (env.sandbox / "factorial.py").read_text() == code
        assert "syntax check passed" in reply.text
    finally:
        env.close()


def test_traceback_explanation_uses_the_real_output(paths, tmp_path):
    from tests.ai_fakes import ScriptedProvider

    provider = ScriptedProvider(answer="The variable x was never defined.")
    env = make_env(paths, tmp_path, ai_provider_factory=lambda _c: provider)
    try:
        reply = env.say("explain this traceback: Traceback (most recent call last):\n  File \"a.py\", line 1\n"
                        "NameError: name 'x' is not defined")
        assert "never defined" in reply.text
        assert "NameError" in provider.requests[-1].messages[-1].content
    finally:
        env.close()


def test_pdf_reading(env):
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with open(env.sandbox / "blank.pdf", "wb") as fh:
        writer.write(fh)
    task = env.run(Step("files.read", {"path": str(env.sandbox / "blank.pdf")}))
    assert task.status is TaskStatus.FAILED and "no text layer" in task.steps[0].error  # honest about scans
