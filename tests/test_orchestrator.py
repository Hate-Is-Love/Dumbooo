"""End-to-end interaction flows through the orchestrator, with fake audio services."""

import numpy as np
import pytest

from app.event_bus import EventBus, Events
from app.orchestrator import ERROR_RECOVERY_S, WAKE_ANIMATION_S, Orchestrator
from ai import errors as AE
from ai.config import AIConfig
from ai.controller import ConversationController
from tests.ai_fakes import FakeTTS, ScriptedProvider
from app.state_machine import AssistantState as S, StateMachine
from tests.fakes import FakeClock, RecordingBus

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


class FakePipeline:
    def __init__(self):
        self.mode, self.carry = "wake", False

    def set_mode(self, mode, carry_over=False):
        self.mode, self.carry = mode, carry_over


class FakeWake:
    def __init__(self):
        self.armed = True

    def arm(self):
        self.armed = True

    def disarm(self):
        self.armed = False


class FakeSTT:
    def __init__(self):
        self.enabled = True
        self.requests = []

    def transcribe_async(self, audio, request_id=0):
        self.requests.append(request_id)


class Harness:
    def __init__(self, initial=S.SLEEPING, tts_ready=True, dashboard=None, provider=None, streaming=True):
        self.clock = FakeClock()
        self.bus = EventBus(synchronous=True)
        self.rec = RecordingBus(self.bus)
        self.sm = StateMachine(self.bus, initial, clock=self.clock)
        self.pipeline, self.wake, self.stt, self.tts = FakePipeline(), FakeWake(), FakeSTT(), FakeTTS(tts_ready)
        self.dashboard = dashboard or {"auto_sleep": True, "sleep_timeout_s": 60, "text_only": False}
        self.provider = provider or ScriptedProvider()
        self.conversation = ConversationController(
            self.bus, AIConfig(provider="echo", streaming=streaming), ROOT,
            provider_factory=lambda _config: self.provider, run_inline=True, clock=self.clock)
        self.orch = Orchestrator(self.bus, self.sm, self.pipeline, self.wake, self.stt, self.tts,
                                 self.conversation, lambda: self.dashboard, clock=self.clock)
        self.orch.start()

    @property
    def state(self):
        return self.sm.state

    def emit(self, name, **payload):
        self.bus.publish(name, payload)

    def tick(self, seconds):
        self.clock.advance(seconds)
        self.orch.tick()

    def say(self):
        self.emit(Events.SPEECH_STARTED)
        self.emit(Events.UTTERANCE_CAPTURED, audio=np.zeros(16000, np.float32), duration=1.0)


@pytest.fixture
def h():
    return Harness()


def test_wake_word_flow_to_listening(h):
    assert h.pipeline.mode == "wake" and h.wake.armed
    h.emit(Events.WAKE_DETECTED, engine="fake", score=0.9, followup="")
    assert h.state == S.WAKING and h.pipeline.mode == "listen" and not h.wake.armed
    h.tick(WAKE_ANIMATION_S + 0.01)
    assert h.state == S.LISTENING


def test_speaking_during_wake_animation_skips_ahead(h):
    h.emit(Events.WAKE_DETECTED, engine="fake", score=0.9, followup="")
    h.emit(Events.SPEECH_STARTED)
    assert h.state == S.LISTENING


def test_complete_voice_interaction(h):
    h.emit(Events.WAKE_DETECTED, engine="fake", score=0.9, followup="")
    h.say()
    assert h.state == S.PROCESSING and h.pipeline.mode == "off"
    request = h.stt.requests[-1]
    h.emit(Events.TRANSCRIPT_READY, text="what time is it", request_id=request)
    assert h.rec.of(Events.USER_TEXT)[-1]["text"] == "what time is it"
    response = h.rec.of(Events.RESPONSE_READY)[-1]
    assert response["text"] == "Answer to: what time is it."
    assert h.rec.of(Events.RESPONSE_DELTA), "answer should stream"
    # text arrived, but audio has not started: STREAMING, never SPEAKING yet
    assert h.state == S.STREAMING and h.tts.spoken == ["Answer to: what time is it."]
    assert 1 in h.tts.ended
    h.emit(Events.PLAYBACK_STARTED, utterance_id=1)
    assert h.state == S.SPEAKING and h.pipeline.mode == "speaking"
    h.emit(Events.PLAYBACK_FINISHED, utterance_id=1, interrupted=False)
    assert h.state == S.IDLE and h.pipeline.mode == "wake" and h.wake.armed


def test_interruption_stops_speech_and_listens(h):
    h.sm.force(S.IDLE)
    h.emit(Events.USER_TEXT, text="tell me a story", source="typed")
    h.emit(Events.PLAYBACK_STARTED, utterance_id=1)
    assert h.state == S.SPEAKING
    h.emit(Events.INTERRUPTION_DETECTED, speech_ms=300)
    assert h.state == S.INTERRUPTED and h.tts.stops >= 1
    assert h.pipeline.mode == "listen" and h.pipeline.carry  # user's first words are kept
    # the old utterance finishing late must not change state
    h.emit(Events.PLAYBACK_FINISHED, utterance_id=1, interrupted=True)
    assert h.state == S.INTERRUPTED
    h.emit(Events.SPEECH_STARTED)
    assert h.state == S.LISTENING


def test_interrupted_falls_back_to_listening_after_hold(h):
    h.sm.force(S.SPEAKING)
    h.emit(Events.INTERRUPTION_DETECTED)
    h.tick(0.5)
    assert h.state == S.LISTENING


def test_interruption_ignored_when_not_speaking(h):
    h.sm.force(S.IDLE)
    h.emit(Events.INTERRUPTION_DETECTED)
    assert h.state == S.IDLE and h.tts.stops == 0


def test_text_input_without_tts_shows_response_and_idles():
    h = Harness(S.IDLE, tts_ready=False)
    h.emit(Events.USER_TEXT, text="hello", source="typed")
    assert h.rec.of(Events.RESPONSE_READY) and h.state == S.IDLE


def test_text_only_mode_never_speaks():
    h = Harness(S.IDLE, dashboard={"auto_sleep": False, "sleep_timeout_s": 60, "text_only": True})
    h.emit(Events.USER_TEXT, text="hello", source="typed")
    assert h.tts.spoken == [] and h.state == S.IDLE


def test_stt_failure_enters_error_then_recovers(h):
    h.emit(Events.WAKE_DETECTED, engine="fake", score=0.9, followup="")
    h.say()
    h.emit(Events.TRANSCRIPT_FAILED, error="model crashed", request_id=h.stt.requests[-1])
    assert h.state == S.ERROR
    assert any(p["source"] == "stt" for p in h.rec.of(Events.ERROR))
    h.tick(ERROR_RECOVERY_S + 0.1)
    assert h.state == S.IDLE


def test_nothing_recognised_returns_to_idle(h):
    h.emit(Events.WAKE_DETECTED, engine="fake", score=0.9, followup="")
    h.say()
    h.emit(Events.TRANSCRIPT_FAILED, error="I couldn't make out any words.", request_id=h.stt.requests[-1], empty=True)
    assert h.state == S.IDLE


def test_stale_transcript_is_ignored(h):
    h.emit(Events.WAKE_DETECTED, engine="fake", score=0.9, followup="")
    h.say()
    old = h.stt.requests[-1]
    h.emit(Events.SLEEP_REQUESTED, source="test")
    h.emit(Events.TRANSCRIPT_READY, text="late", request_id=old)
    assert h.state == S.SLEEPING and not h.rec.of(Events.USER_TEXT)


def test_listen_timeout_returns_to_idle(h):
    h.emit(Events.WAKE_DETECTED, engine="fake", score=0.9, followup="")
    h.tick(1)
    h.emit(Events.LISTEN_TIMEOUT)
    assert h.state == S.IDLE


def test_auto_sleep_after_idle_timeout(h):
    h.emit(Events.WAKE_REQUESTED, source="tray")
    h.tick(1)
    assert h.state == S.IDLE
    h.tick(30)
    assert h.state == S.IDLE
    h.tick(31)
    assert h.state == S.SLEEPING and h.wake.armed and h.pipeline.mode == "wake"


def test_long_answer_does_not_sleep_the_moment_it_ends():
    # Regression: web research ran 70 s, and DUMBOO fell asleep 0.15 s after returning to idle.
    h = Harness(S.IDLE)
    h.sm.transition(S.PROCESSING, "thinking")
    h.tick(90)
    assert h.state == S.PROCESSING
    h.emit(Events.STOP_SPEAKING, source="ui")
    h.tick(1)
    assert h.state == S.IDLE
    h.tick(61)
    assert h.state == S.SLEEPING


def test_auto_sleep_can_be_disabled():
    h = Harness(S.IDLE, dashboard={"auto_sleep": False, "sleep_timeout_s": 10, "text_only": False})
    h.tick(1000)
    assert h.state == S.IDLE


def test_the_sign_in_screen_never_puts_itself_to_sleep():
    # Regression: DUMBOO hid its sign-in screen in the tray after 60 s, before anyone could sign in.
    h = Harness(S.IDLE)
    signed_in = [False]
    h.orch._can_auto_sleep = lambda: signed_in[0]
    h.tick(600)
    assert h.state == S.IDLE
    signed_in[0] = True
    h.tick(61)
    assert h.state == S.SLEEPING


def test_manual_wake_goes_to_idle_not_listening(h):
    h.emit(Events.WAKE_REQUESTED, source="hotkey")
    assert h.pipeline.mode == "off"
    h.emit(Events.SPEECH_STARTED)  # someone talking in the room during the wake animation
    assert h.state == S.WAKING
    h.tick(1)
    assert h.state == S.IDLE and h.pipeline.mode == "wake"


def test_duplicate_wake_while_awake_is_ignored(h):
    h.emit(Events.WAKE_DETECTED, engine="fake", score=0.9, followup="")
    h.emit(Events.WAKE_DETECTED, engine="fake", score=0.9, followup="")
    changes = [p["new"] for p in h.rec.of(Events.STATE_CHANGED)]
    assert changes == ["WAKING"]


def test_wake_with_command_in_one_breath(h):
    h.emit(Events.WAKE_DETECTED, engine="whisper_spotter", score=0.9, followup="what is on my schedule")
    texts = h.rec.of(Events.USER_TEXT)
    assert texts and texts[0]["text"] == "what is on my schedule" and texts[0]["source"] == "voice"
    assert h.rec.of(Events.RESPONSE_READY)


def test_talk_button_while_speaking_stops_and_listens(h):
    h.sm.force(S.SPEAKING)
    h.emit(Events.LISTEN_REQUESTED, source="ui")
    assert h.state == S.LISTENING and h.tts.stops >= 1


def test_stop_button(h):
    h.sm.force(S.SPEAKING)
    h.emit(Events.STOP_SPEAKING, source="ui")
    assert h.state == S.IDLE and h.tts.stops >= 1


def test_microphone_loss_while_listening_is_an_error(h):
    h.emit(Events.WAKE_DETECTED, engine="fake", score=0.9, followup="")
    h.emit(Events.MIC_STATUS, open=False, device="USB mic", error="Microphone disconnected")
    assert h.state == S.ERROR and h.sm.last_error == "Microphone disconnected"


def test_sleep_from_any_awake_state(h):
    for state in (S.IDLE, S.LISTENING, S.PROCESSING, S.SPEAKING, S.INTERRUPTED, S.ERROR):
        h.sm.force(state)
        h.emit(Events.SLEEP_REQUESTED, source="test")
        assert h.state == S.SLEEPING, state


def test_response_provider_failure_shows_error_and_retry_recovers():
    h = Harness(S.IDLE, provider=ScriptedProvider(errors=[AE.AuthError("bad key")]))
    h.emit(Events.USER_TEXT, text="hi", source="typed")
    assert h.state == S.ERROR
    failed = h.rec.of(Events.RESPONSE_FAILED)[-1]
    assert failed["kind"] == "auth" and failed["error"] == "bad key"
    h.emit(Events.RETRY_REQUESTED)
    assert h.rec.of(Events.RESPONSE_READY)[-1]["text"] == "Answer to: hi."
    assert len(h.rec.of(Events.AI_STARTED)) == 2


def test_typed_then_spoken_share_context():
    h = Harness(S.IDLE)
    h.emit(Events.USER_TEXT, text="Tell me about Python", source="typed")
    h.emit(Events.PLAYBACK_STARTED, utterance_id=1)
    h.emit(Events.PLAYBACK_FINISHED, utterance_id=1, interrupted=False)
    assert h.state == S.IDLE
    h.emit(Events.WAKE_DETECTED, engine="fake", score=0.9, followup="")
    h.say()
    h.emit(Events.TRANSCRIPT_READY, text="What are its advantages?", request_id=h.stt.requests[-1])
    sent = h.provider.requests[-1].messages
    assert [m.content for m in sent] == ["Tell me about Python", "Answer to: Tell me about Python.",
                                         "What are its advantages?"]


def test_new_message_while_streaming_cancels_old_answer():
    import threading

    gate = threading.Event()
    h = Harness(S.IDLE, provider=ScriptedProvider(gate=gate))
    h.conversation._inline = False  # let the first request block in a worker thread
    h.emit(Events.USER_TEXT, text="first question", source="typed")
    first_seq = h.rec.of(Events.AI_STARTED)[-1]["seq"]
    h.emit(Events.USER_TEXT, text="second question", source="typed")
    gate.set()
    from tests.fakes import wait_for
    assert wait_for(lambda: h.rec.of(Events.RESPONSE_READY))
    ready = h.rec.of(Events.RESPONSE_READY)
    assert [r["text"] for r in ready] == ["Answer to: second question."]
    assert any(c["seq"] == first_seq for c in h.rec.of(Events.RESPONSE_CANCELLED))
    assert all(d["seq"] != first_seq for d in h.rec.of(Events.RESPONSE_DELTA))


def test_stop_during_streaming_cancels_generation_and_speech():
    import threading

    gate = threading.Event()
    h = Harness(S.IDLE, provider=ScriptedProvider(gate=gate))
    h.conversation._inline = False
    h.emit(Events.USER_TEXT, text="long story", source="typed")
    assert h.state == S.PROCESSING
    h.emit(Events.STOP_SPEAKING, source="ui")
    gate.set()
    assert h.state == S.IDLE and h.tts.stops >= 1
    from tests.fakes import wait_for
    assert wait_for(lambda: h.rec.of(Events.RESPONSE_CANCELLED))
    import time
    time.sleep(0.1)
    assert not h.rec.of(Events.RESPONSE_READY)  # the cancelled answer never shows up
    assert h.conversation.context.turn_count == 0


def test_duplicate_submission_is_ignored():
    h = Harness(S.IDLE)
    h.emit(Events.USER_TEXT, text="hello", source="typed")
    h.emit(Events.USER_TEXT, text="hello", source="typed")
    assert len(h.rec.of(Events.AI_STARTED)) == 1
    h.clock.advance(5)
    h.emit(Events.USER_TEXT, text="hello", source="typed")
    assert len(h.rec.of(Events.AI_STARTED)) == 2


def test_non_streaming_mode_speaks_whole_answer():
    h = Harness(S.IDLE, streaming=False)
    h.emit(Events.USER_TEXT, text="hi", source="typed")
    assert not h.rec.of(Events.RESPONSE_DELTA)
    assert h.tts.spoken == ["Answer to: hi."] and h.state == S.PROCESSING
    h.emit(Events.PLAYBACK_STARTED, utterance_id=1)
    assert h.state == S.SPEAKING


def test_markdown_answer_is_spoken_clean():
    h = Harness(S.IDLE, provider=ScriptedProvider(pieces=["**RAM** is ", "*temporary* memory.\n\n", "```py\n",
                                                          "a = 1\nb = 2\nc = 3\n", "```\n", "See [docs](http://x.io)."]))
    h.emit(Events.USER_TEXT, text="what is ram", source="typed")
    spoken = " ".join(h.tts.spoken)
    assert "**" not in spoken and "```" not in spoken and "http" not in spoken
    assert "RAM is temporary memory." in spoken and "I've put the code on screen." in spoken
    assert h.rec.of(Events.RESPONSE_READY)[-1]["text"].startswith("**RAM**")  # display keeps Markdown


def test_clear_conversation_forgets_context():
    h = Harness(S.IDLE)
    h.emit(Events.USER_TEXT, text="remember me", source="typed")
    assert h.conversation.context.turn_count == 1
    h.emit(Events.CONVERSATION_CLEARED)
    assert h.conversation.context.turn_count == 0 and h.state == S.IDLE


def test_using_the_dashboard_keeps_dumboo_awake(h):
    # Regression: clicking around ACCOUNT/SETTINGS didn't count, so DUMBOO hid itself mid face setup.
    h.emit(Events.WAKE_REQUESTED, source="tray")
    h.tick(1)
    for _ in range(4):
        h.tick(40)
        h.emit(Events.UI_ACTIVITY)
    assert h.state == S.IDLE
    h.tick(61)
    assert h.state == S.SLEEPING
