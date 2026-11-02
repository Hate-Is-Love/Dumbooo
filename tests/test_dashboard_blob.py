"""The audio-reactive 3D blob in the core: rendering, voice reaction, sensitivity, interaction, settings."""

import numpy as np
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from dashboard.blob import PointBlob
from dashboard.core_animation import STATE_TARGETS, CoreWidget


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def run(core, state, level=0.0, frames=40):
    core.set_state(state)
    for _ in range(frames):
        if level:
            core.set_level(level)
        core._advance()
        core._last_ms -= 33  # pretend 33 ms passed per frame


def test_blob_renders_glowing_dots_in_the_phase_colours(app):
    blob = PointBlob()
    image = blob.render(120, QColor("#3fe0e8"), QColor("#9b8cff"), 1.0)
    pixels = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(image.height(), image.width(), 4)
    lit = pixels[:, :, 3] > 40
    assert lit.sum() > 3000                        # thousands of visible dots
    top = pixels[: image.height() // 3][lit[: image.height() // 3]]
    bottom = pixels[-image.height() // 3:][lit[-image.height() // 3:]]
    assert top[:, 1].mean() > bottom[:, 1].mean()  # cyan (green-rich) at the top, violet at the bottom


def test_voice_makes_the_blob_swell_more_with_higher_sensitivity(app):
    amps = {}
    for sensitivity in (0.5, 2.0):
        core = CoreWidget()
        core.set_blob(True, sensitivity)
        run(core, "LISTENING", level=0.15)
        amps[sensitivity] = core._blob.amp
    quiet = CoreWidget()
    run(quiet, "LISTENING")
    assert amps[2.0] > amps[0.5] > quiet._blob.amp


def test_every_phase_still_has_its_animation(app):
    for state in STATE_TARGETS:
        core = CoreWidget()
        core.resize(500, 460)
        run(core, state, frames=10)
        image = QImage(500, 460, QImage.Format.Format_ARGB32)
        image.fill(0)
        core.render(image)  # rings, arcs, panels and labels all paint without errors in every phase


def test_click_on_the_blob_means_talk_and_drag_spins_it(app):
    core = CoreWidget()
    core.resize(600, 520)
    core.show()
    run(core, "IDLE", frames=3)
    core.repaint()
    QApplication.processEvents()  # the first paint tells the widget where the blob is
    assert core._blob_radius > 0
    talks = []
    core.talk_requested.connect(lambda: talks.append(1))
    centre = core._blob_center.toPoint()
    QTest.mouseClick(core, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre)
    assert talks == [1]
    yaw = core._blob.yaw
    QTest.mousePress(core, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre)
    QTest.mouseMove(core, centre + QPoint(60, 10))
    QTest.mouseRelease(core, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre + QPoint(60, 10))
    assert core._blob.yaw != yaw and talks == [1]  # a drag is not a click
    QTest.mouseClick(core, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(5, 5))
    assert talks == [1]                              # outside the blob: nothing
    core.set_blob(False, 1.0)
    core.repaint()
    QTest.mouseClick(core, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre)
    assert talks == [1]                              # blob off: the original orb, no click-to-talk
    core.hide()


def test_settings_page_and_defaults_include_the_blob(app, paths):
    from dashboard.dashboard_controller import DashboardController
    from dashboard.widgets.settings_panel import SettingsPanel
    from tests.test_application import make_core

    core = make_core(paths)
    try:
        assert core.settings.get("dashboard", "blob_enabled") is True
        assert core.settings.get("dashboard", "blob_sensitivity") == 1.0
        panel = SettingsPanel(DashboardController(core))
        panel.blob_sensitivity.set_value(1.5)
        panel.blob_enabled.setChecked(False)
        dash = panel.collect()["dashboard"]
        assert dash["blob_sensitivity"] == 1.5 and dash["blob_enabled"] is False
    finally:
        core.shutdown()


def _swell(core, state, source, noise, speech, rate_hz=10, seconds=5):
    """Amplitude of the blob during background noise (1-2 s) and during speech (last 2 s)."""
    import math
    core.set_blob(True, 1.0)
    core.set_state(state)
    quiet, talk, every = [], [], max(1, round(30 / rate_hz))
    for f in range(30 * seconds):
        t = f / 30
        if f % every == 0:
            syllable = 0.5 + 0.5 * math.sin(t * 2 * math.pi * 4)
            core.set_level(speech * (0.3 + 0.7 * syllable) if t >= 2 else noise, source)
        core._advance()
        core._last_ms -= 33
        if 1 < t < 2:
            quiet.append(core._blob.amp)
        elif t > 3:
            talk.append(core._blob.amp)
    return sum(quiet) / len(quiet), sum(talk) / len(talk)


def test_a_quiet_laptop_microphone_still_moves_the_blob_clearly(app):
    quiet, talk = _swell(CoreWidget(), "LISTENING", "voice", noise=0.001, speech=0.006)
    assert quiet < 0.09            # steady room noise leaves it calm
    assert talk > 0.2 and talk > 2.5 * quiet


def test_the_blob_reacts_to_dumboo_speaking(app):
    quiet, talk = _swell(CoreWidget(), "SPEAKING", "bot", noise=0.0, speech=0.08, rate_hz=20)
    assert quiet < 0.09 and talk > 0.3


def test_the_reaction_is_held_between_level_updates_instead_of_flickering(app):
    core = CoreWidget()
    core.set_blob(True, 1.0)
    core.set_state("LISTENING")
    core.set_level(0.05)
    target = core._level_target
    for _ in range(3):              # 100 ms without a new level (the microphone sends ~10 per second)
        core._advance(); core._last_ms -= 33
    assert core._level_target == target
    for _ in range(15):             # half a second of silence: it does relax
        core._advance(); core._last_ms -= 33
    assert core._level_target < target * 0.2
