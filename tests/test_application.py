"""Headless start-up, end-to-end text conversation, metrics and graceful shutdown."""

import threading
from types import SimpleNamespace

import pytest

from app.application import AssistantCore
from app.event_bus import Events
from app.state_machine import AssistantState as S
from services.system_monitor import SystemMonitor
from tests.fakes import (AmplitudeVAD, FakeInputStream, FakeOutputStream, FakeSynth, FakeWhisperModel, RecordingBus,
                         wait_for)


class _Engine:
    engine, detail = "fake", "fake engine"

    def __init__(self, cb):
        self.cb = cb

    def process(self, *a):
        pass

    def reset(self):
        pass

    def close(self):
        pass


class MemoryTokenStore:
    """Stand-in for the DPAPI token file: keeps the token in memory only."""

    def __init__(self):
        self.token = None

    def save(self, token):
        self.token = token
        return True

    def load(self):
        return self.token

    def clear(self):
        self.token = None


OWNER = ("owner", "Owner Display", "Correct-Horse-42")


def make_core(paths, signed_in: bool = True, **kw):
    """A headless DUMBOO with fake hardware. By default the test owner is set up and signed in."""
    from security.passwords import make_hasher

    FakeInputStream.reset()
    FakeOutputStream.instances = []
    defaults = dict(mic_factory=FakeInputStream, speaker_factory=FakeOutputStream,
                    synth_factory=lambda p, s: FakeSynth(), whisper_loader=lambda name, **k: FakeWhisperModel("hi"),
                    vad=AmplitudeVAD(), wake_engine_factory=_Engine,
                    environ={"AI_PROVIDER": "echo"},  # isolated: never reads real keys, never calls an API
                    password_hasher=make_hasher(fast=True), token_store=MemoryTokenStore())
    if "desktop" not in kw:  # never touch the real desktop or browser in tests
        from tests.automation_fakes import FakeBrowser, FakeDesktop, FakeUIA, fake_capture
        desktop = FakeDesktop(paths.data_dir / "trash")
        defaults.update(desktop=desktop, browser=FakeBrowser(), uia=FakeUIA(desktop), screen_capture=fake_capture)
    if "research_providers" not in kw:  # research never reaches the internet in tests
        from tests.research_fakes import FakeSearchProvider, fake_resolver, offline_client
        from tests.automation_fakes import FakeBrowser as _FB
        defaults.update(research_providers={"tavily": FakeSearchProvider("tavily"), "brave": FakeSearchProvider("brave"),
                                            "google_news": FakeSearchProvider("google_news", kinds=("news",)),
                                            "gdelt": FakeSearchProvider("gdelt", kinds=("news",)),
                                            "browser": FakeSearchProvider("browser")},
                        research_client=offline_client(), research_resolver=fake_resolver, research_browser=_FB())
    if "guardian_overrides" not in kw:  # the Security Guardian never reads or changes the real system in tests
        from tests.guardian_fakes import harmless_overrides
        defaults["guardian_overrides"] = harmless_overrides()
    if "weather_client" not in kw:  # weather never reaches the internet in tests
        from tests.everyday_fakes import FakeWeatherHTTP
        defaults["weather_client"] = FakeWeatherHTTP()
    defaults.update(kw)
    core = AssistantCore(paths, **defaults)
    if "desktop" not in kw:  # no network access from tests either
        core.automation.services.probe = lambda url: (True, "the site answered (HTTP 200, test)")
        core.monitor.network = lambda check_internet=True: {"interfaces_up": ["Test"], "internet": True,
                                                            "latency_ms": 5}
    if signed_in:
        if core.auth.needs_owner_setup():
            core.accounts.setup_owner(OWNER[0], OWNER[1], OWNER[2])
        else:
            core.accounts.login(OWNER[0], OWNER[2])
    return core


@pytest.fixture
def core(paths):
    c = make_core(paths)
    yield c
    c.shutdown()


def test_startup_starts_services_in_standby(core):
    rec = RecordingBus(core.bus)
    core.start()
    assert core.state.state == S.SLEEPING
    assert core.pipeline.running and core.mic.is_open and core.wake.loaded
    assert core.wake_listener_running()
    assert core.reminders.running
    assert wait_for(lambda: core.tts.ready and core.stt.ready)
    status = core.service_status()
    assert set(status) == {"microphone", "speaker", "wake_word", "vad", "stt", "tts", "ai", "database", "account",
                           "reminders", "automation", "research"}
    assert status["automation"]["ok"]
    assert status["account"]["ok"] and status["database"]["ok"]
    assert status["wake_word"]["ok"] and status["tts"]["ok"]
    assert rec is not None


def test_typed_message_round_trip_with_speech(core):
    rec = RecordingBus(core.bus)
    core.start()
    core.bus.publish(Events.WAKE_REQUESTED, {"source": "test"})
    assert wait_for(lambda: core.state.state == S.IDLE and core.tts.ready, 3)
    core.bus.publish(Events.USER_TEXT, {"text": "hello dumboo", "source": "typed"})
    assert wait_for(lambda: rec.of(Events.PLAYBACK_FINISHED), 5)
    assert wait_for(lambda: core.state.state == S.IDLE, 3)
    seen = [p["new"] for p in rec.of(Events.STATE_CHANGED)]
    assert seen[-4:] == ["PROCESSING", "STREAMING", "SPEAKING", "IDLE"]
    assert "hello dumboo" in rec.of(Events.RESPONSE_READY)[0]["text"]


def test_app_runs_without_microphone(paths):
    FakeInputStream.reset()
    FakeInputStream.fail = True
    core = make_core(paths)
    FakeInputStream.fail = True  # make_core resets the flag
    rec = RecordingBus(core.bus)
    core.start()
    assert not core.pipeline.mic_ok
    assert not core.wake_listener_running()  # never claims to listen without a microphone
    assert wait_for(lambda: any(not p["open"] for p in rec.of(Events.MIC_STATUS)), 3)  # the bus delivers async
    # text interaction still works
    core.bus.publish(Events.USER_TEXT, {"text": "typing works", "source": "typed"})
    assert wait_for(lambda: rec.of(Events.RESPONSE_READY), 3)
    core.shutdown()
    FakeInputStream.reset()


def test_text_only_mode_never_opens_microphone(paths):
    core = make_core(paths, text_only=True)
    core.start()
    assert FakeInputStream.instances == [] and not core.pipeline.running
    assert core.service_status()["wake_word"]["detail"] == "off (text-only mode)"
    core.shutdown()


def test_graceful_shutdown_stops_every_thread(paths):
    before = {t.name for t in threading.enumerate()}
    core = make_core(paths)
    core.start()
    core.shutdown()
    core.shutdown()  # idempotent
    assert core.state.state == S.SHUTTING_DOWN
    assert not core.pipeline.running and not core.mic.is_open and not core.reminders.running
    assert FakeInputStream.instances[0].closed
    leftover = {t.name for t in threading.enumerate() if t.is_alive()} - before
    assert not {"AudioPipeline", "Reminders", "Ticker", "TTS", "EventBus"} & leftover, leftover


def test_settings_change_is_applied_live(core):
    core.start()
    core.settings.update({"audio": {"noise_suppression": False}, "vad": {"end_silence_ms": 1500},
                          "interruption": {"enabled": False}})
    assert core.suppressor.enabled is False
    assert core.pipeline.segmenter.config.end_silence_ms == 1500
    assert core.pipeline.interruption_enabled is False


def test_wake_detection_reaches_listening_through_real_bus(core):
    core.start()
    core.wake._engine.cb(0.9, "")
    assert wait_for(lambda: core.state.state == S.LISTENING, 3)
    assert core.pipeline.mode == "listen" and not core.wake.armed


# ---------------------------------------------------------------- system metrics
def test_system_metrics_are_real_numbers():
    snap = SystemMonitor().sample()
    assert 0 <= snap.cpu_percent <= 100
    assert 0 < snap.ram_percent <= 100 and snap.ram_total_gb > 0
    assert snap.os_name and snap.uptime_s >= 0


def test_missing_battery_is_reported_unavailable_not_invented():
    fake_ps = SimpleNamespace(
        cpu_percent=lambda interval=None: 12.0,
        virtual_memory=lambda: SimpleNamespace(percent=40.0, used=4 * 1024**3, total=10 * 1024**3),
        sensors_battery=lambda: None,
    )
    snap = SystemMonitor(ps=fake_ps).sample()
    assert snap.battery_percent is None and snap.battery_charging is None
    assert snap.cpu_percent == 12.0


def test_metric_errors_become_unavailable():
    def boom(*a, **k):
        raise PermissionError("denied")

    fake_ps = SimpleNamespace(cpu_percent=boom, virtual_memory=boom, sensors_battery=boom)
    snap = SystemMonitor(ps=fake_ps).sample()
    assert snap.cpu_percent is None and snap.ram_percent is None and snap.battery_percent is None


def test_app_stays_awake_on_the_sign_in_screen(paths):
    core = make_core(paths)
    core.accounts.logout() if core.accounts.session is not None else None
    assert core.accounts.session is None
    assert core.orchestrator._can_auto_sleep() is False   # the sign-in screen is never hidden by the idle timer
    core.shutdown()
