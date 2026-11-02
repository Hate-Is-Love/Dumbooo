"""Central assistant state machine.

Every visible behaviour of DUMBOO (the core animation, the dashboard status
labels, which audio consumer receives microphone frames) is derived from the
single state stored here. Other modules request transitions; illegal
transitions are refused and logged instead of silently corrupting the state.
"""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum

from app.event_bus import EventBus, Events

log = logging.getLogger(__name__)


class AssistantState(str, Enum):
    SLEEPING = "SLEEPING"
    WAKING = "WAKING"
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    STREAMING = "STREAMING"   # the AI answer is arriving; TTS has not started playing yet
    SPEAKING = "SPEAKING"
    INTERRUPTED = "INTERRUPTED"
    ERROR = "ERROR"
    SHUTTING_DOWN = "SHUTTING_DOWN"


S = AssistantState

# Allowed transitions. SHUTTING_DOWN is reachable from everywhere and is final.
TRANSITIONS: dict[AssistantState, frozenset[AssistantState]] = {
    S.SLEEPING: frozenset({S.WAKING, S.PROCESSING, S.ERROR}),
    S.WAKING: frozenset({S.LISTENING, S.IDLE, S.PROCESSING, S.SLEEPING, S.ERROR}),
    S.IDLE: frozenset({S.LISTENING, S.PROCESSING, S.SLEEPING, S.ERROR}),
    S.LISTENING: frozenset({S.PROCESSING, S.IDLE, S.SLEEPING, S.ERROR}),
    S.PROCESSING: frozenset({S.STREAMING, S.SPEAKING, S.IDLE, S.LISTENING, S.SLEEPING, S.ERROR}),
    S.STREAMING: frozenset({S.SPEAKING, S.IDLE, S.PROCESSING, S.LISTENING, S.SLEEPING, S.ERROR}),
    S.SPEAKING: frozenset({S.INTERRUPTED, S.IDLE, S.PROCESSING, S.SLEEPING, S.ERROR}),
    S.INTERRUPTED: frozenset({S.LISTENING, S.IDLE, S.ERROR}),
    S.ERROR: frozenset({S.IDLE, S.SLEEPING, S.LISTENING, S.PROCESSING}),  # a new message / RETRY
    S.SHUTTING_DOWN: frozenset(),
}

# States in which the dashboard is considered "awake".
AWAKE_STATES = frozenset({S.WAKING, S.IDLE, S.LISTENING, S.PROCESSING, S.STREAMING, S.SPEAKING, S.INTERRUPTED,
                          S.ERROR})


class StateMachine:
    def __init__(self, bus: EventBus, initial: AssistantState = S.SLEEPING, clock=time.monotonic) -> None:
        self._bus = bus
        self._state = initial
        self._clock = clock
        self._entered_at = clock()
        self._lock = threading.RLock()
        self.last_error: str | None = None

    @property
    def state(self) -> AssistantState:
        with self._lock:
            return self._state

    @property
    def time_in_state(self) -> float:
        with self._lock:
            return self._clock() - self._entered_at

    def can_transition(self, new: AssistantState) -> bool:
        with self._lock:
            if new == S.SHUTTING_DOWN:
                return self._state != S.SHUTTING_DOWN
            return new in TRANSITIONS[self._state]

    def transition(self, new: AssistantState, reason: str = "") -> bool:
        """Move to ``new`` if allowed. Returns True when the state changed."""
        with self._lock:
            old = self._state
            if old == new:
                return False
            if not self.can_transition(new):
                log.debug("Refused transition %s -> %s (%s)", old.value, new.value, reason)
                return False
            self._state = new
            self._entered_at = self._clock()
            if new == S.ERROR:
                self.last_error = reason
        log.info("State %s -> %s %s", old.value, new.value, f"({reason})" if reason else "")
        self._bus.publish(Events.STATE_CHANGED, {"old": old.value, "new": new.value, "reason": reason})
        return True

    def force(self, new: AssistantState, reason: str = "") -> None:
        """Recovery path: move to ``new`` even when not normally reachable.

        Only used by the orchestrator to recover from ERROR or an abandoned
        interaction; never reaches back out of SHUTTING_DOWN.
        """
        with self._lock:
            if self._state == S.SHUTTING_DOWN:
                return
            old = self._state
            if old == new:
                return
            self._state = new
            self._entered_at = self._clock()
        log.warning("Forced state %s -> %s (%s)", old.value, new.value, reason)
        self._bus.publish(Events.STATE_CHANGED, {"old": old.value, "new": new.value, "reason": reason})
