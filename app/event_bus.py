"""Thread-safe publish/subscribe event bus.

Audio threads, worker threads and the UI never call each other directly. They
publish events here. In the running application the bus delivers events on its
own dispatcher thread, in the order they were published, so every handler runs
serially and no handler can block an audio callback. Tests construct the bus
with ``synchronous=True`` so events are delivered immediately and
deterministically.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)


class Events:
    """Names of all events used in Phase 1. Payloads are documented inline."""

    STATE_CHANGED = "state.changed"            # {"old": str, "new": str, "reason": str}
    WAKE_DETECTED = "wake.detected"            # {"engine": str, "score": float, "phrase": str}
    WAKE_REQUESTED = "wake.requested"          # {"source": "tray"|"hotkey"|"ui"}
    LISTEN_REQUESTED = "listen.requested"      # {"source": "ui"} (the "Talk" button)
    TICK = "tick"                              # {} every 250 ms; drives timeouts
    SLEEP_REQUESTED = "sleep.requested"        # {"source": str}
    WAKE_SERVICE_STATUS = "wake.status"        # {"running": bool, "engine": str, "detail": str}

    MIC_STATUS = "mic.status"                  # {"open": bool, "device": str, "error": str|None}
    AUDIO_LEVEL = "audio.level"                # {"rms": float, "speech_prob": float}
    SPEECH_STARTED = "speech.started"          # {}
    SPEECH_ENDED = "speech.ended"              # {"duration": float}
    UTTERANCE_CAPTURED = "utterance.captured"  # {"audio": np.ndarray, "duration": float}
    UTTERANCE_REJECTED = "utterance.rejected"  # {"reason": str}
    LISTEN_TIMEOUT = "listen.timeout"          # {}

    TRANSCRIPT_READY = "stt.transcript"        # {"text": str, "language": str, "duration": float}
    TRANSCRIPT_FAILED = "stt.failed"           # {"error": str}
    STT_STATUS = "stt.status"                  # {"ready": bool, "detail": str}

    USER_TEXT = "user.text"                    # {"text": str, "source": "typed"|"voice"}
    AI_STARTED = "ai.started"                  # {"seq", "text", "source", "provider", "model"} (message accepted)
    AI_RETRYING = "ai.retrying"                # {"seq", "attempt", "wait_s", "reason"}
    UI_ACTIVITY = "ui.activity"                # {} the user clicked, typed or scrolled in the dashboard (throttled)
    GREETING = "assistant.greeting"            # {"text", "speech", "method"} said after signing in
    AI_SWITCHED = "ai.switched"                # {"seq", "from", "to", "reason"} another provider/key took over
    AI_STATUS = "ai.status"                    # {"ready", "provider", "model", "detail", "turns"}
    RESPONSE_DELTA = "assistant.delta"         # {"seq", "delta", "text"} streamed piece + text so far
    RESPONSE_READY = "assistant.response"      # {"seq", "text", "speech", "provider", "model", "finish_reason", ...}
    RESPONSE_FAILED = "assistant.failed"       # {"seq", "error", "kind", "partial", "provider"}
    RESPONSE_CANCELLED = "assistant.cancelled" # {"seq", "reason"}
    RETRY_REQUESTED = "assistant.retry"        # {} the RETRY button
    CONVERSATION_CLEARED = "conversation.cleared"  # {}

    # Module 3: accounts, history and memory. AI events also carry "scope" (the Session);
    # the Qt bridge strips it so session tokens never reach widgets.
    USER_SIGNED_IN = "user.signed_in"          # {"username", "display_name", "role", "title", "must_change_password"}
    USER_SIGNED_OUT = "user.signed_out"        # {"reason"}
    PROFILE_CHANGED = "user.profile"           # {"title", "response_length", "voice_output"}
    MEMORY_RETRIEVAL = "memory.retrieval"      # {"seq", "memories", "messages", "phase": "start"|"done"}
    MEMORY_CHANGED = "memory.changed"          # {"action", "count"}
    HISTORY_CHANGED = "history.changed"        # {"conversation_id"}

    # Module 4: laptop automation. Payloads never contain the session or typed/private text.
    AUTOMATION_TASK = "automation.task"        # {"user_id", "task": Task.snapshot()} on every change
    AUTOMATION_CONFIRM = "automation.confirm"  # {"user_id", "task_id", "step_id", "risk", "question", "details"}
    AUTOMATION_STOPPED = "automation.stopped"  # {"source", "completed": [...], "cancelled": [...]}
    AUTOMATION_ANNOUNCE = "automation.announce"  # {"user_id", "task_id", "text"} result of a background task
    SYSTEM_ALERT = "system.alert"              # {"metric", "value", "threshold", "message"}

    # Module 5: research. Payloads carry no session and no page text.
    RESEARCH_PROGRESS = "research.progress"    # {"user_id", "task": {id, title, kind, status, detail, steps:[...]}}
    RESEARCH_DONE = "research.done"            # {"user_id", "research_id", "status", "kind", "query"}

    TTS_STATUS = "tts.status"                  # {"ready": bool, "detail": str}
    PLAYBACK_STARTED = "playback.started"      # {"utterance_id": int}
    PLAYBACK_FINISHED = "playback.finished"    # {"utterance_id": int, "interrupted": bool}
    PLAYBACK_LEVEL = "playback.level"          # {"rms": float}
    STOP_SPEAKING = "playback.stop"            # {"source": str}
    INTERRUPTION_DETECTED = "interruption"     # {"speech_ms": float}

    REMINDER_DUE = "reminder.due"              # {"kind": "event"|"task", "event_id"|"task_id": int, "title", "starts_at", "missed"?}
    SETTINGS_CHANGED = "settings.changed"      # {"settings": dict, "changed": list[str]}
    ERROR = "error"                            # {"source": str, "message": str, "recoverable": bool}
    SHUTDOWN_REQUESTED = "shutdown.requested"  # {"source": str}


Handler = Callable[[str, dict], None]


@dataclass
class _Envelope:
    name: str
    payload: dict = field(default_factory=dict)


_STOP = _Envelope("__stop__")


class EventBus:
    def __init__(self, synchronous: bool = False) -> None:
        self._synchronous = synchronous
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._wildcard: list[Handler] = []
        self._lock = threading.RLock()
        self._queue: "queue.Queue[_Envelope]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = False

    # -- subscription -----------------------------------------------------
    def subscribe(self, name: str, handler: Handler) -> Callable[[], None]:
        """Subscribe to an event name, or ``"*"`` for every event.

        Returns a function that removes the subscription.
        """
        with self._lock:
            target = self._wildcard if name == "*" else self._handlers[name]
            target.append(handler)

        def unsubscribe() -> None:
            with self._lock:
                if handler in target:
                    target.remove(handler)

        return unsubscribe

    # -- publishing -------------------------------------------------------
    def publish(self, name: str, payload: dict | None = None) -> None:
        envelope = _Envelope(name, payload or {})
        if self._synchronous:
            self._dispatch(envelope)
        else:
            self._queue.put(envelope)

    def _dispatch(self, envelope: _Envelope) -> None:
        with self._lock:
            handlers = list(self._handlers.get(envelope.name, ())) + list(self._wildcard)
        for handler in handlers:
            try:
                handler(envelope.name, envelope.payload)
            except Exception:  # a broken subscriber must never kill the bus
                log.exception("Event handler failed for %s", envelope.name)

    # -- dispatcher thread ------------------------------------------------
    def start(self) -> None:
        if self._synchronous or self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="EventBus", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            envelope = self._queue.get()
            if envelope is _STOP:
                break
            self._dispatch(envelope)

    def stop(self, timeout: float = 2.0) -> None:
        if not self._running:
            return
        self._running = False
        self._queue.put(_STOP)
        if self._thread is not None:
            self._thread.join(timeout)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._synchronous or self._running

    def drain(self, timeout: float = 2.0) -> None:
        """Block until queued events have been dispatched (used in tests)."""
        if self._synchronous:
            return
        done = threading.Event()
        unsubscribe = self.subscribe("__drain__", lambda _n, p: p["done"].set())
        self._queue.put(_Envelope("__drain__", {"done": done}))
        done.wait(timeout)
        unsubscribe()


Payload = dict[str, Any]
