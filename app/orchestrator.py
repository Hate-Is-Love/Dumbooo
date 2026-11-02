"""Turns events into state transitions and actions.

All handlers run on the event-bus dispatcher thread, one at a time, so the
orchestrator needs no locks of its own. Slow work (speech recognition, AI
generation, speech synthesis) runs on worker threads and reports back through
events. Each interaction gets a sequence number (``seq``); results that arrive
for an interaction that has since been cancelled or replaced are discarded.

Typed and spoken messages take the same path: ``USER_TEXT`` →
``ConversationController.submit`` → ``RESPONSE_DELTA``… → ``RESPONSE_READY``.
While an answer streams in, finished sentences are passed to TTS straight away.
The state only becomes SPEAKING when TTS reports that audio really started.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from ai.speech_format import SpeechChunker
from app.event_bus import EventBus, Events
from app.state_machine import AssistantState as S
from app.state_machine import StateMachine

log = logging.getLogger(__name__)

WAKE_ANIMATION_S = 0.8
INTERRUPTED_HOLD_S = 0.4
ERROR_RECOVERY_S = 6.0

# Which audio consumer receives microphone frames in each state.
PIPELINE_MODE = {
    S.SLEEPING: "wake",
    S.IDLE: "wake",
    S.ERROR: "wake",
    S.WAKING: "listen",  # "off" instead after a manual wake (see _apply_pipeline_mode)
    S.LISTENING: "listen",
    S.INTERRUPTED: "listen",
    S.PROCESSING: "off",
    S.STREAMING: "off",
    S.SPEAKING: "speaking",
    S.SHUTTING_DOWN: "off",
}

BUSY_STATES = (S.PROCESSING, S.STREAMING, S.SPEAKING)

AI_ERROR_REASONS = {
    "config": "AI not configured", "auth": "AI key rejected", "quota": "AI quota used up",
    "rate_limit": "AI rate limit", "model": "AI model unavailable", "timeout": "AI timed out",
    "network": "no connection to AI", "unavailable": "AI service busy", "blocked": "answer blocked",
    "empty": "empty answer", "too_long": "conversation too long",
}


class Orchestrator:
    name = "orchestrator"

    def __init__(self, bus: EventBus, sm: StateMachine, pipeline, wake, stt, tts, conversation,
                 dashboard_settings: Callable[[], dict], clock=time.monotonic,
                 scope_provider: Callable[[], object] = lambda: None,
                 voice_allowed: Callable[[], bool] = lambda: True,
                 can_auto_sleep: Callable[[], bool] = lambda: True) -> None:
        self.bus = bus
        self.sm = sm
        self.pipeline = pipeline
        self.wake = wake
        self.stt = stt
        self.tts = tts
        self.conversation = conversation
        self._dashboard = dashboard_settings
        self._scope = scope_provider          # the signed-in user's Session (Module 3)
        self._voice_allowed = voice_allowed   # the signed-in user's "voice output" preference
        self._can_auto_sleep = can_auto_sleep  # False on the sign-in screen: it must not hide itself
        self._clock = clock
        self._seq = 0
        self._utterance_id: int | None = None   # TTS utterance for the current answer
        self._chunker: SpeechChunker | None = None
        self._answer_complete = False
        self._listen_after = False
        self._wake_target = S.LISTENING
        self._last_activity = clock()
        self._unsubscribers: list[Callable[[], None]] = []

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        handlers = {
            Events.STATE_CHANGED: self._on_state_changed,
            Events.WAKE_DETECTED: self._on_wake_detected,
            Events.WAKE_REQUESTED: self._on_wake_requested,
            Events.SLEEP_REQUESTED: self._on_sleep_requested,
            Events.LISTEN_REQUESTED: self._on_listen_requested,
            Events.SPEECH_STARTED: self._on_speech_started,
            Events.UTTERANCE_CAPTURED: self._on_utterance,
            Events.LISTEN_TIMEOUT: self._on_listen_timeout,
            Events.TRANSCRIPT_READY: self._on_transcript,
            Events.TRANSCRIPT_FAILED: self._on_transcript_failed,
            Events.USER_TEXT: self._on_user_text,
            Events.RESPONSE_DELTA: self._on_response_delta,
            Events.RESPONSE_READY: self._on_response,
            Events.RESPONSE_FAILED: self._on_response_failed,
            Events.RETRY_REQUESTED: self._on_retry,
            Events.CONVERSATION_CLEARED: self._on_cleared,
            Events.PLAYBACK_STARTED: self._on_playback_started,
            Events.PLAYBACK_FINISHED: self._on_playback_finished,
            Events.INTERRUPTION_DETECTED: self._on_interruption,
            Events.STOP_SPEAKING: self._on_stop,
            Events.MIC_STATUS: self._on_mic_status,
            Events.TICK: lambda _n, _p: self.tick(),
            # Using the dashboard (pages, settings, face setup) is activity too: don't sleep and hide meanwhile.
            Events.UI_ACTIVITY: lambda _n, _p: self._touch(),
        }
        for name, handler in handlers.items():
            self._unsubscribers.append(self.bus.subscribe(name, handler))
        self._apply_pipeline_mode(self.sm.state)

    def stop(self) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()

    # -- helpers ---------------------------------------------------------------
    @property
    def state(self) -> S:
        return self.sm.state

    def _touch(self) -> None:
        self._last_activity = self._clock()

    def _new_interaction(self) -> int:
        self._seq += 1
        return self._seq

    def _go(self, state: S, reason: str = "") -> bool:
        return self.sm.transition(state, reason)

    def _apply_pipeline_mode(self, state: S, carry_over: bool = False) -> None:
        mode = PIPELINE_MODE[state]
        if state == S.WAKING and self._wake_target != S.LISTENING:
            mode = "off"  # a tray/hotkey wake only shows the dashboard; room chatter must not start listening
        if self.pipeline is not None:
            self.pipeline.set_mode(mode, carry_over=carry_over)
        if self.wake is not None:
            if state in (S.SLEEPING, S.IDLE, S.ERROR):
                self.wake.arm()
            else:
                self.wake.disarm()

    def _voice_output_enabled(self) -> bool:
        return not self._dashboard().get("text_only", False) and self._voice_allowed()

    def _stop_answer(self, reason: str) -> None:
        """Cancel generation and speech for the current interaction."""
        self._utterance_id = None
        self._chunker = None
        self._answer_complete = False
        if self.conversation is not None:
            self.conversation.cancel(reason)
        if self.tts is not None:
            self.tts.stop_speaking()

    def _report(self, message: str, source: str, recoverable: bool = True) -> None:
        self.bus.publish(Events.ERROR, {"source": source, "message": message, "recoverable": recoverable})

    # -- state side effects ------------------------------------------------------
    def _on_state_changed(self, _name: str, payload: dict) -> None:
        new = S(payload["new"])
        self._apply_pipeline_mode(new, carry_over=(new == S.INTERRUPTED))
        # Idle time counts from the moment DUMBOO became idle, so a long answer (e.g. web research
        # over a minute) doesn't put it to sleep the instant it finishes.
        self._touch()

    # -- wake / sleep ----------------------------------------------------------------
    def _on_wake_detected(self, _name: str, payload: dict) -> None:
        if self.state not in (S.SLEEPING, S.IDLE, S.ERROR):
            return
        followup = (payload.get("followup") or "").strip()
        if len(followup.split()) >= 2:
            # "Hey DUMBOO, what's on my schedule" spoken in one breath.
            self._wake_target = S.IDLE
            if self.state == S.SLEEPING:
                self._go(S.WAKING, "wake phrase")
            self.bus.publish(Events.USER_TEXT, {"text": followup, "source": "voice"})
            return
        self._wake_target = S.LISTENING
        if self.state == S.SLEEPING:
            self._go(S.WAKING, "wake phrase")
        else:
            self._go(S.LISTENING, "wake phrase")

    def _on_wake_requested(self, _name: str, payload: dict) -> None:
        if self.state == S.SLEEPING:
            self._wake_target = S.IDLE
            self._go(S.WAKING, f"manual wake ({payload.get('source', 'ui')})")
        self._touch()

    def _on_sleep_requested(self, _name: str, payload: dict) -> None:
        self._new_interaction()
        self._stop_answer("going to sleep")
        if not self._go(S.SLEEPING, f"sleep ({payload.get('source', 'ui')})"):
            self.sm.force(S.SLEEPING, "sleep requested")

    def _on_listen_requested(self, _name: str, _payload: dict) -> None:
        if self.state in BUSY_STATES:
            self._new_interaction()
            self._stop_answer("talk button")
            if self.state == S.SPEAKING:
                self._go(S.INTERRUPTED, "talk button")
            self.sm.force(S.LISTENING, "talk button")
            return
        if self.state == S.SLEEPING:
            self._wake_target = S.LISTENING
            self._go(S.WAKING, "talk button")
            return
        if self.state in (S.IDLE, S.ERROR):
            self._go(S.LISTENING, "talk button")

    # -- listening ---------------------------------------------------------------------
    def _on_speech_started(self, _name: str, _payload: dict) -> None:
        self._touch()
        if self.state == S.INTERRUPTED or (self.state == S.WAKING and self._wake_target == S.LISTENING):
            self._go(S.LISTENING, "speech started")

    def _on_utterance(self, _name: str, payload: dict) -> None:
        if self.state not in (S.LISTENING, S.WAKING, S.INTERRUPTED):
            return
        if self.state != S.LISTENING:
            self._go(S.LISTENING, "speech captured")
        seq = self._new_interaction()
        if self.stt is None or not self.stt.enabled:
            self._go(S.IDLE, "no speech recognition")
            self._report("Speech recognition is disabled or unavailable. You can still type.", "stt")
            return
        self._go(S.PROCESSING, "transcribing")
        self.stt.transcribe_async(payload["audio"], request_id=seq)

    def _on_listen_timeout(self, _name: str, _payload: dict) -> None:
        if self.state in (S.LISTENING, S.WAKING):
            self._go(S.IDLE, "no speech heard")

    def _on_transcript(self, _name: str, payload: dict) -> None:
        if payload.get("request_id") != self._seq or self.state != S.PROCESSING:
            return
        self.bus.publish(Events.USER_TEXT, {"text": payload["text"], "source": "voice", "request_id": self._seq})

    def _on_transcript_failed(self, _name: str, payload: dict) -> None:
        if payload.get("request_id") != self._seq or self.state != S.PROCESSING:
            return
        if payload.get("empty"):
            self._go(S.IDLE, "nothing recognised")
        else:
            self._go(S.ERROR, payload.get("error", "speech recognition failed"))
        self._report(payload.get("error", "Speech recognition failed"), "stt")

    # -- answering (typed and spoken messages share this path) ---------------------------
    def _on_user_text(self, _name: str, payload: dict) -> None:
        text = (payload.get("text") or "").strip()
        if not text or self.conversation is None:
            return
        self._touch()
        source = payload.get("source", "typed")
        # A transcript already owns the current seq; anything else starts a new interaction.
        voice_of_current = source == "voice" and payload.get("request_id") == self._seq
        if not self.conversation.accepts(text, source):
            if voice_of_current and self.state == S.PROCESSING:
                self._go(S.IDLE, "duplicate message ignored")
            return
        seq = self._seq if voice_of_current else self._seq + 1
        self._seq = seq
        self._begin_answer(seq)  # record the turn before any answer event can arrive
        self.conversation.submit(text, source, seq, scope=self._scope())

    def _begin_answer(self, seq: int) -> None:
        if self.tts is not None and self.state == S.SPEAKING:
            self.tts.stop_speaking()
        self._utterance_id = None
        self._chunker = None
        self._answer_complete = False
        if self.state != S.PROCESSING and not self._go(S.PROCESSING, "thinking"):
            self.sm.force(S.PROCESSING, "thinking")

    def _on_retry(self, _name: str, _payload: dict) -> None:
        if self.conversation is None or not self.conversation.can_retry:
            return
        seq = self._new_interaction()
        self._begin_answer(seq)
        if not self.conversation.retry(seq):
            self._go(S.IDLE, "nothing to retry")

    def _on_cleared(self, _name: str, _payload: dict) -> None:
        self._new_interaction()
        self._stop_answer("conversation cleared")
        if self.state in BUSY_STATES or self.state == S.ERROR:
            self.sm.force(S.IDLE, "conversation cleared")
        if self.conversation is not None:
            self.conversation.clear()

    def _speech_wanted(self) -> bool:
        return self.tts is not None and self._voice_output_enabled() and getattr(self.tts, "available", True)

    def _on_response_delta(self, _name: str, payload: dict) -> None:
        if payload.get("seq") != self._seq or self.state not in BUSY_STATES:
            return
        if self.state == S.PROCESSING:
            self._go(S.STREAMING, "answer arriving")
        if not self._speech_wanted():
            return
        if self._utterance_id is None and self._chunker is None:
            self._utterance_id = self.tts.begin_stream()
            self._chunker = SpeechChunker() if self._utterance_id is not None else None
        if self._chunker is not None and self._utterance_id is not None:
            for sentence in self._chunker.push(payload.get("delta", "")):
                self.tts.feed(self._utterance_id, sentence)

    def _on_response(self, _name: str, payload: dict) -> None:
        if payload.get("seq") != self._seq or self.state not in BUSY_STATES:
            return
        self._answer_complete = True
        # A spoken question (e.g. an automation confirmation) is followed by listening, so a voice user
        # can simply say "yes" or "no" without the wake phrase.
        self._listen_after = bool(payload.get("listen_after")) and payload.get("source") == "voice"
        if self._chunker is not None and self._utterance_id is not None:
            for sentence in self._chunker.flush():
                self.tts.feed(self._utterance_id, sentence)
            self.tts.end_stream(self._utterance_id)
            self._chunker = None
            return  # SPEAKING / IDLE follow from playback events
        if self._utterance_id is None and self._speech_wanted():
            utterance = self.tts.speak(payload.get("speech") or payload["text"])
            if utterance is not None:
                self._utterance_id = utterance
                return
        if self.state != S.SPEAKING:
            self._go(S.IDLE, "answer shown")

    def _on_response_failed(self, _name: str, payload: dict) -> None:
        if payload.get("seq") != self._seq:
            return
        self._utterance_id = None
        self._chunker = None
        if self.tts is not None:
            self.tts.stop_speaking()
        # Short reason for the state display; the full explanation is shown in the conversation.
        self._go(S.ERROR, AI_ERROR_REASONS.get(payload.get("kind", ""), "AI request failed"))
        self._report(payload.get("error", "AI request failed"), "assistant")

    # -- speaking --------------------------------------------------------------------------
    def _on_playback_started(self, _name: str, payload: dict) -> None:
        if payload.get("utterance_id") == self._utterance_id and self.state in (S.PROCESSING, S.STREAMING):
            self._go(S.SPEAKING, "playback started")

    def _on_playback_finished(self, _name: str, payload: dict) -> None:
        if self._utterance_id is None or payload.get("utterance_id") != self._utterance_id:
            return  # an utterance we already cancelled
        self._utterance_id = None
        if not self._answer_complete:
            # Speech ended before the text did (should not happen with streaming TTS): keep showing text.
            self._chunker = None
            return
        if self.state in BUSY_STATES:
            self._go(S.IDLE, "playback finished")
            if self._listen_after:
                self._listen_after = False
                self._go(S.LISTENING, "waiting for your answer")

    def _on_interruption(self, _name: str, _payload: dict) -> None:
        if self.state != S.SPEAKING:
            return
        self._new_interaction()
        self._stop_answer("user interrupted")
        self._go(S.INTERRUPTED, "user interrupted")

    def _on_stop(self, _name: str, _payload: dict) -> None:
        if self.state in BUSY_STATES:
            self._new_interaction()
            self._stop_answer("stopped by user")
            self._go(S.IDLE, "stopped by user")

    # -- devices -------------------------------------------------------------------------------
    def _on_mic_status(self, _name: str, payload: dict) -> None:
        if payload.get("open") or not payload.get("error"):
            return
        if self.state in (S.LISTENING, S.WAKING, S.INTERRUPTED):
            self._go(S.ERROR, payload["error"])

    # -- timers (TICK) ---------------------------------------------------------------------------
    def tick(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        state, held = self.state, self.sm.time_in_state
        if state == S.WAKING and held >= WAKE_ANIMATION_S:
            self._go(self._wake_target, "wake animation done")
        elif state == S.INTERRUPTED and held >= INTERRUPTED_HOLD_S:
            self._go(S.LISTENING, "listening after interruption")
        elif state == S.ERROR and held >= ERROR_RECOVERY_S:
            self._go(S.IDLE, "recovered")
        elif state == S.IDLE:
            dashboard = self._dashboard()
            if (dashboard.get("auto_sleep") and now - self._last_activity >= float(dashboard.get("sleep_timeout_s", 60))
                    and self._can_auto_sleep()):
                self._go(S.SLEEPING, "idle timeout")
