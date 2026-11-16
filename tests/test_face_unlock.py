"""Face unlock: the sign-in and setup checks, storage and lockout, the login page, and the greeting."""

from datetime import datetime

import numpy as np
import pytest

from ai.greeting import build_greeting
from app.event_bus import Events
from security.face import MATCH, FaceEnrollment, FaceObservation, FaceSignIn
from services.auth_service import InvalidCredentials
from services.face_service import MAX_FAILURES, FaceError
from tests.fakes import FakeClock, wait_for
from tests.test_history_memory import Rig

RNG = np.random.default_rng(5)


def unit(v):
    return (v / np.linalg.norm(v)).astype(np.float32)


ME = unit(RNG.normal(size=128))
STRANGER = unit(RNG.normal(size=128))


def looks(person, noise=0.15):
    return unit(person + RNG.normal(size=128) * noise / np.sqrt(128) * 4)


def obs(person=ME, yaw=0.0, faces=1, size=160, score=0.95, noise=0.15):
    return FaceObservation((100, 80, size, size), score, yaw, looks(person, noise), faces)


def identify_against(*people):
    def identify(embedding):
        scores = [(i + 1, float(np.dot(p, embedding))) for i, p in enumerate(people)]
        user, score = max(scores, key=lambda s: s[1])
        return user, score
    return identify


# -- the sign-in check ---------------------------------------------------------------------------------
def test_recognised_face_that_turns_and_looks_back_signs_in():
    clock = FakeClock()
    check = FaceSignIn(identify_against(ME), clock=clock)
    steps = [check.feed(obs()) for _ in range(3)]
    assert steps[-1].stage == "turn"
    assert check.feed(obs(yaw=0.30)).stage == "back"
    final = check.feed(obs(yaw=0.02))
    assert final.done and final.ok and final.user_id == 1


def test_a_photo_cannot_pass_because_it_never_turns():
    clock = FakeClock()
    check = FaceSignIn(identify_against(ME), clock=clock)
    for _ in range(40):
        clock.advance(0.6)
        result = check.feed(obs(yaw=0.01, noise=0.02))  # the same still picture
    assert result.done and not result.ok and "timed out" in result.message


def test_an_unknown_face_is_refused():
    clock = FakeClock()
    check = FaceSignIn(identify_against(ME), clock=clock)
    for _ in range(10):
        clock.advance(1.0)
        result = check.feed(obs(person=STRANGER))
    assert result.done and not result.ok and "not recognised" in result.message


def test_another_person_cannot_finish_someone_elses_check():
    check = FaceSignIn(identify_against(ME), clock=FakeClock())
    for _ in range(3):
        check.feed(obs())
    swapped = check.feed(obs(person=STRANGER, yaw=0.3))  # someone else steps in while turning
    assert swapped.stage == "turn" and not swapped.done


@pytest.mark.parametrize("observation,message", [
    (None, "Look at the camera"), (obs(faces=2), "Only one face"), (obs(size=40), "closer")])
def test_clear_instructions(observation, message):
    assert message in FaceSignIn(identify_against(ME), clock=FakeClock()).feed(observation).message


# -- the setup check ---------------------------------------------------------------------------------------
def test_setup_collects_front_and_both_sides_of_one_face():
    clock = FakeClock()
    setup = FaceEnrollment(clock=clock)
    plan = [0.0] * 4 + [0.25] * 2 + [-0.25] * 2
    for yaw in plan:
        clock.advance(0.5)
        result = setup.feed(obs(yaw=yaw))
    assert result.done and result.ok
    assert setup.embeddings().shape == (8, 128)


def test_setup_ignores_a_second_person():
    clock = FakeClock()
    setup = FaceEnrollment(clock=clock)
    clock.advance(0.5)
    setup.feed(obs())
    clock.advance(0.5)
    assert "same face" in setup.feed(obs(person=STRANGER)).message
    assert len(setup.samples) == 1


# -- storage, matching, lockout (real database, real DPAPI encryption) --------------------------------------
@pytest.fixture
def rig(paths):
    r = Rig(paths)
    yield r
    r.close()


def enroll(rig, person=ME, password=None):
    from tests.test_application import OWNER
    vectors = np.stack([looks(person) for _ in range(8)])
    return rig.core.face.enroll(rig.session, password or OWNER[2], vectors)


def test_setup_needs_the_password_and_stores_only_encrypted_numbers(rig):
    with pytest.raises(InvalidCredentials):
        enroll(rig, password="wrong password")
    assert enroll(rig) == 8
    row = rig.core.db.row("SELECT embeddings FROM face_profiles")
    raw = bytes(row["embeddings"])
    assert len(raw) > 8 * 128 * 4 and np.frombuffer(raw[:512], dtype=np.uint8).std() > 0  # DPAPI blob
    assert not np.allclose(np.frombuffer(raw[:4096], dtype=np.float32)[:8], np.stack([ME])[0][:8])
    assert rig.core.face.status(rig.session)["enrolled"]


def test_face_sign_in_opens_a_session_and_greets(rig):
    enroll(rig)
    identify = rig.core.face.matcher()
    user_id, score = identify(looks(ME))
    assert score >= MATCH
    assert wait_for(lambda: rig.rec.of(Events.GREETING))  # the greeting of the first (password) sign-in
    before = len(rig.rec.of(Events.GREETING))
    rig.core.accounts.logout()
    rig.core.accounts.login_with_face(user_id)
    assert wait_for(lambda: len(rig.rec.of(Events.GREETING)) > before)
    greeting = rig.rec.of(Events.GREETING)[-1]
    assert greeting["method"] == "face" and greeting["text"].startswith("Face recognised.")
    assert rig.rec.of(Events.USER_SIGNED_IN)[-1]["method"] == "face"


def test_a_stranger_does_not_match(rig):
    enroll(rig)
    _user, score = rig.core.face.matcher()(looks(STRANGER))
    assert score < MATCH


def test_too_many_failures_pause_face_unlock_until_a_password_sign_in(rig):
    from tests.test_application import OWNER
    enroll(rig)
    for _ in range(MAX_FAILURES):
        rig.core.face.record_failure("not recognised")
    with pytest.raises(FaceError):
        rig.core.face.matcher()
    rig.core.accounts.logout()
    rig.core.accounts.login(OWNER[0], OWNER[2])  # a password sign-in lifts the pause
    assert rig.core.face.matcher() is not None


def test_owner_can_switch_face_unlock_off(rig):
    enroll(rig)
    policy = rig.core.face._policy
    rig.core.face._policy = lambda: {**policy(), "face_unlock_enabled": False}
    assert not rig.core.face.any_enrolled()
    with pytest.raises(FaceError):
        rig.core.face.sign_in(1)


def test_removing_face_data(rig):
    enroll(rig)
    assert rig.core.face.remove(rig.session)
    assert not rig.core.face.status(rig.session)["enrolled"]


# -- greeting ----------------------------------------------------------------------------------------------
def test_first_greeting_introduces_dumboo_and_later_ones_are_short():
    evening = datetime(2026, 10, 22, 19, 0)
    first, spoken = build_greeting("Sir", evening, first_time=True)
    assert first.startswith("Good evening, Sir.") and "I'm DUMBOO" in first and "reminders" in first
    later, _ = build_greeting("Sir", datetime(2026, 10, 23, 8, 0), first_time=False, method="face", reminders_today=2)
    assert later == ("Face recognised. Good morning, Sir. Welcome back. I'm DUMBOO, ready when you are. "
                     "You have 2 reminders today.")


def test_greeting_happens_once_as_introduction_then_short_and_can_be_switched_off(rig):
    from tests.test_application import OWNER
    assert wait_for(lambda: rig.rec.of(Events.GREETING))
    assert "your personal desktop assistant" in rig.rec.of(Events.GREETING)[0]["text"]
    rig.core.accounts.logout()
    rig.core.accounts.login(OWNER[0], OWNER[2])
    assert wait_for(lambda: len(rig.rec.of(Events.GREETING)) == 2)
    assert "Welcome back" in rig.rec.of(Events.GREETING)[1]["text"]
    rig.core.profile.set(rig.session, "greeting", "off")
    rig.core.accounts.logout()
    rig.core.accounts.login(OWNER[0], OWNER[2])
    assert not wait_for(lambda: len(rig.rec.of(Events.GREETING)) == 3, 1.0)


# -- the login page with a fake camera ------------------------------------------------------------------------
class FakeCamera:
    def __init__(self):
        self.released = False

    def read(self):
        return True, np.zeros((480, 640, 3), dtype=np.uint8)

    def release(self):
        self.released = True


class ScriptedEngine:
    """Plays the observations of someone looking, turning and looking back."""

    available = True
    error = None

    def __init__(self, person=ME):
        self.frames = [obs(person)] * 3 + [obs(person, yaw=0.3)] + [obs(person, yaw=0.0)] * 30
        self.i = 0

    def observe(self, _frame):
        self.i += 1
        return self.frames[min(self.i - 1, len(self.frames) - 1)]


def test_login_page_face_button_signs_in_and_turns_the_camera_off(paths):
    from PySide6.QtWidgets import QApplication

    from dashboard.dashboard_controller import DashboardController
    from dashboard.widgets.login_page import LoginPage

    app = QApplication.instance() or QApplication([])
    camera = FakeCamera()
    r = Rig(paths, face_engine=ScriptedEngine(), camera_factory=lambda: camera)
    try:
        enroll(r)
        r.core.accounts.logout()
        page = LoginPage(DashboardController(r.core))
        page.refresh()
        assert not page.face_button.isHidden()
        page._face_login()
        assert wait_for(lambda: (app.processEvents(), r.core.accounts.signed_in)[1], 10)
        assert camera.released
        # the event bus is asynchronous: wait for the face sign-in event instead of reading the latest one at once
        assert wait_for(lambda: any(e["method"] == "face" for e in r.rec.of(Events.USER_SIGNED_IN)), 5)
    finally:
        r.close()


def test_face_button_is_hidden_until_someone_set_it_up(paths):
    from PySide6.QtWidgets import QApplication

    from dashboard.dashboard_controller import DashboardController
    from dashboard.widgets.login_page import LoginPage

    QApplication.instance() or QApplication([])
    r = Rig(paths, face_engine=ScriptedEngine(), camera_factory=FakeCamera)
    try:
        r.core.accounts.logout()
        page = LoginPage(DashboardController(r.core))
        page.refresh()
        assert page.face_button.isHidden()
    finally:
        r.close()


def test_real_models_load_and_see_no_face_in_an_empty_picture():
    from pathlib import Path

    from security.face import FaceEngine

    engine = FaceEngine(Path(__file__).resolve().parent.parent / "models" / "face")
    if not engine.available:
        pytest.skip(engine.error)
    assert engine.observe(np.zeros((480, 640, 3), dtype=np.uint8)) is None


def test_dashboard_clicks_report_activity_and_face_checks_are_logged(paths, caplog):
    import logging

    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from dashboard.dashboard_controller import DashboardController
    from dashboard.face_session import FaceSessionWorker
    from dashboard.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    r = Rig(paths)
    try:
        window = MainWindow(DashboardController(r.core))
        window.show()
        app.processEvents()
        before = len(r.rec.of(Events.UI_ACTIVITY))
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(50, 50))
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(60, 60))
        assert wait_for(lambda: len(r.rec.of(Events.UI_ACTIVITY)) == before + 1)  # throttled: one per 2 s
        window.hide()
        with caplog.at_level(logging.INFO, logger="dashboard.face_session"):
            worker = FaceSessionWorker(ScriptedEngine(person=STRANGER), FaceSignIn(lambda e: (None, 0.0),
                                                                                    timeout_s=0.3), FakeCamera)
            worker.start()
            worker.wait()
        text = caplog.text
        assert "Face check started (FaceSignIn)" in text and "Face check finished (FaceSignIn): failed" in text
        assert "camera off" in text
    finally:
        r.close()


# -- learning from confirmed sign-ins (threshold unchanged) -------------------------------------------------
def _stored(rig):
    face = rig.core.face
    row = rig.core.db.row("SELECT embeddings FROM face_profiles")
    return np.frombuffer(face._unprotect(bytes(row["embeddings"])), dtype=np.float32).reshape(-1, 128)


def test_a_confirmed_sign_in_teaches_new_lighting_but_never_another_face(rig):
    enroll(rig)
    user_id = rig.core.auth.require(rig.session).id
    originals = _stored(rig).copy()
    face = rig.core.face
    # the same person in different light: still above MATCH against the setup samples, but not a near-copy
    other_light = unit(ME + RNG.normal(size=128) * 0.09)
    assert MATCH <= float(np.max(originals @ other_light)) < 0.92
    assert face._learn(user_id, [other_light]) == 1
    stored = _stored(rig)
    assert stored.shape[0] == 9 and np.allclose(stored[:8], originals)       # setup samples untouched
    assert face._learn(user_id, [STRANGER]) == 0                              # a different face is never learned
    assert face._learn(user_id, [stored[0]]) == 0                             # a near-copy adds nothing
    for _ in range(20):                                                       # learned samples are capped
        face._learn(user_id, [unit(ME + RNG.normal(size=128) * 0.09)])
    assert _stored(rig).shape[0] <= 8 + 12 and np.allclose(_stored(rig)[:8], originals)


def test_a_face_matching_another_account_is_not_learned(rig, monkeypatch):
    enroll(rig)
    owner = rig.core.auth.require(rig.session).id
    face = rig.core.face
    other_light = unit(ME + RNG.normal(size=128) * 0.09)
    real_profiles = face._profiles
    # another active account whose stored face is close to this frame (e.g. a sibling)
    monkeypatch.setattr(face, "_profiles", lambda exclude=None: real_profiles(exclude) + [(owner + 1, np.stack(
        [other_light]))])
    assert face._learn(owner, [other_light]) == 0
    assert _stored(rig).shape[0] == 8


def test_sign_in_returns_the_confirmed_frames():
    check = FaceSignIn(identify_against(ME), clock=FakeClock())
    for _ in range(3):
        check.feed(obs())
    check.feed(obs(yaw=0.30))
    final = check.feed(obs(yaw=0.02))
    assert final.ok and len(final.samples) == 2 and final.best >= MATCH
