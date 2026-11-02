"""Dashboard smoke tests (offscreen Qt): every state renders, panels work, UI stays fast."""

import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QPoint  # noqa: E402
from PySide6.QtGui import QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.event_bus import Events  # noqa: E402
from dashboard.core_animation import STATE_TARGETS, CoreWidget  # noqa: E402
from tests.fakes import wait_for  # noqa: E402
from tests.test_application import make_core  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def pump(ms=50):
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def test_core_renders_every_state_quickly(qapp):
    w = CoreWidget()
    w.resize(900, 700)
    w.set_tags([("STATE", "IDLE"), ("MIC", "OPEN")])
    image = QImage(900, 700, QImage.Format.Format_ARGB32)
    frames = []
    for state in STATE_TARGETS:
        w.set_state(state)
        w.set_level(0.1)
        for _ in range(5):
            w._advance()
            start = time.perf_counter()
            painter = QPainter(image)
            w.render(painter, QPoint())
            painter.end()
            frames.append(time.perf_counter() - start)
    median = sorted(frames)[len(frames) // 2]
    assert median < 0.033, f"median frame {median * 1000:.1f} ms"  # within a 30 fps budget
    lit = sum(1 for x in range(0, 900, 15) for y in range(0, 700, 15) if image.pixelColor(x, y).value() > 60)
    assert lit > 20  # something was actually drawn


def test_core_stops_animating_when_hidden(qapp):
    w = CoreWidget()
    w.show()
    assert w.animating
    w.hide()
    assert not w.animating


def test_main_window_interaction(qapp, paths):
    from dashboard.dashboard_controller import DashboardController
    from dashboard.main_window import MainWindow

    core = make_core(paths)
    controller = DashboardController(core)
    window = MainWindow(controller)
    core.start()
    try:
        controller.wake("test")
        assert wait_for(lambda: (pump(20), window.isVisible())[1], 3)
        convo = window.assistant.conversation
        convo.input.setText("hello from the keyboard")
        convo._submit()
        assert wait_for(lambda: (pump(20), convo.message_count() >= 2)[1], 5)
        assert convo.input.text() == ""
        assert wait_for(lambda: (pump(20), window.assistant.core.state() == "IDLE")[1], 5)

        # tasks panel round trip through the real database
        core.tasks.create(core.accounts.session, "Write README", priority="high")
        window.tasks.refresh()
        assert wait_for(lambda: (pump(20), window.tasks.pending.rowCount() == 1)[1], 3)

        # system panel shows real metrics
        window.system.refresh()
        assert wait_for(lambda: (pump(20), window.system.cpu.value.text() not in ("—", ""))[1], 3)

        # going to sleep hides the window but keeps the wake listener alive
        controller.sleep("test")
        assert wait_for(lambda: (pump(20), core.state.state.value == "SLEEPING")[1], 3)
        pump(50)
        if window.tray is not None:
            assert not window.isVisible()
        assert core.wake_listener_running()

        # closing the window with X means sleep, not quit
        controller.wake("test")
        assert wait_for(lambda: (pump(20), window.isVisible())[1], 3)
        window.close()
        assert wait_for(lambda: (pump(20), core.state.state.value == "SLEEPING")[1], 3)
    finally:
        window._quitting = True
        window.close()
        controller.shutdown()


def test_reminder_event_shows_banner(qapp, paths):
    from dashboard.dashboard_controller import DashboardController
    from dashboard.main_window import MainWindow

    core = make_core(paths)
    controller = DashboardController(core)
    window = MainWindow(controller)
    core.start()
    try:
        core.bus.publish(Events.REMINDER_DUE, {"event_id": 1, "title": "Dentist", "starts_at": "2026-10-21T15:00"})
        assert wait_for(lambda: (pump(20), "Dentist" in window.assistant.banner.text())[1], 3)
    finally:
        window._quitting = True
        window.close()
        controller.shutdown()


# ============================================================ Module 2: AI conversation in the dashboard
def _window(paths, **core_kw):
    from dashboard.dashboard_controller import DashboardController
    from dashboard.main_window import MainWindow

    core = make_core(paths, **core_kw)
    controller = DashboardController(core)
    window = MainWindow(controller)
    core.start()
    controller.wake("test")
    assert wait_for(lambda: (pump(20), window.isVisible() and core.tts.ready)[1], 5)
    return core, controller, window


def _close(window, controller):
    window._quitting = True
    window.close()
    controller.shutdown()


def test_streaming_answer_and_animation_follow_real_events(qapp, paths):
    import threading

    from tests.ai_fakes import ScriptedProvider

    gate = threading.Event()
    provider = ScriptedProvider(pieces=["RAM is ", "temporary ", "memory."], gate=gate)
    core, controller, window = _window(paths, ai_provider_factory=lambda _c: provider)
    states = []
    core.bus.subscribe(Events.STATE_CHANGED, lambda _n, p: states.append(p["new"]))
    playback = []
    core.bus.subscribe(Events.PLAYBACK_STARTED, lambda _n, p: playback.append(list(states)))
    try:
        convo = window.assistant.conversation
        convo.input.setText("What is RAM?")
        convo._submit()
        # processing animation starts right after submission, before any answer exists
        assert wait_for(lambda: (pump(20), window.assistant.core.state() == "PROCESSING")[1], 3)
        assert convo.answers() and convo.answers()[-1].text == ""
        gate.set()
        assert wait_for(lambda: (pump(20), "SPEAKING" in states)[1], 5)
        answer = convo.answers()[-1]
        assert wait_for(lambda: (pump(20), answer.done)[1], 5)
        assert answer.text == "RAM is temporary memory."
        # SPEAKING only after TTS really started; STREAMING came first
        assert playback and "SPEAKING" not in playback[0] and "STREAMING" in playback[0]
        assert wait_for(lambda: (pump(20), window.assistant.core.state() == "IDLE")[1], 5)
        assert states.index("SPEAKING") < len(states) - 1 and states[-1] == "IDLE"  # speaking stopped at the end
    finally:
        _close(window, controller)


def test_provider_failure_shows_error_state_and_retry(qapp, paths):
    from ai import errors as E
    from tests.ai_fakes import ScriptedProvider

    provider = ScriptedProvider(errors=[E.QuotaError("Gemini quota for test-model is used up (daily limit).")])
    core, controller, window = _window(paths, ai_provider_factory=lambda _c: provider)
    try:
        convo = window.assistant.conversation
        convo.input.setText("hello")
        convo._submit()
        assert wait_for(lambda: (pump(20), window.assistant.core.state() == "ERROR")[1], 5)
        from PySide6.QtWidgets import QPushButton
        retry = [b for b in convo.findChildren(QPushButton) if "RETRY" in b.text()]
        assert retry, "an error bubble with a RETRY button is shown"
        retry[0].click()
        assert wait_for(lambda: (pump(20), convo.answers()[-1].done and convo.answers()[-1].text)[1], 5)
        assert convo.answers()[-1].text == "Answer to: hello."
    finally:
        _close(window, controller)


def test_text_only_mode_conversation_without_audio(qapp, paths):
    from tests.fakes import FakeInputStream, FakeOutputStream

    core, controller, window = _window(paths, text_only=True)
    try:
        convo = window.assistant.conversation
        convo.input.setText("typing only")
        convo._submit()
        assert wait_for(lambda: (pump(20), bool(convo.answers()) and convo.answers()[-1].done)[1], 5)
        assert "typing only" in convo.answers()[-1].text
        assert FakeInputStream.instances == [] and FakeOutputStream.instances == []  # no mic, no speaker
        assert wait_for(lambda: (pump(20), core.state.state.value == "IDLE")[1], 3)
    finally:
        _close(window, controller)
