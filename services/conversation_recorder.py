"""Automatically saves every typed and spoken exchange to the signed-in user's history.

Listens to the conversation events (``AI_STARTED``, ``RESPONSE_READY``,
``RESPONSE_FAILED``, ``RESPONSE_CANCELLED``). Each event carries the Session
that was active when the message was accepted, so a message is always stored
under the user who sent it, even if someone signs in a moment later.

All database writes run on one background thread, in event order, so the bus
and the dashboard never wait for the disk.

* user message → saved when accepted (``pending``)
* answer ready → assistant message saved (``completed``), user message ``completed``
* failure → user message ``failed`` (+ error kind); any partial text saved as ``interrupted``
* cancel / interruption → user message ``interrupted``; partial text saved as ``interrupted``
* retry → the original user message is reused (no duplicate row)
* nothing is saved without a signed-in user
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.event_bus import EventBus, Events
from services.auth_service import AuthError, Session
from services.history_service import HistoryService

log = logging.getLogger(__name__)


class ConversationRecorder:
    name = "recorder"

    def __init__(self, bus: EventBus, history: HistoryService, memory_assistant=None) -> None:
        self._bus = bus
        self._history = history
        self._memory_assistant = memory_assistant
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._turns: dict[int, dict[str, Any]] = {}          # seq -> {"scope", "key", "message_id", ...}
        self._conversation: dict[str, int] = {}               # session token hash -> conversation id
        self._unsubscribe: list = []

    # -- lifecycle ---------------------------------------------------------------
    def start(self) -> None:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="HistoryWriter")
        for name, handler in ((Events.AI_STARTED, self._on_started), (Events.RESPONSE_READY, self._on_ready),
                              (Events.RESPONSE_FAILED, self._on_failed), (Events.RESPONSE_CANCELLED, self._on_cancelled)):
            self._unsubscribe.append(self._bus.subscribe(name, handler))

    def stop(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None

    def flush(self, timeout: float = 5.0) -> None:
        """Wait until every queued write is done (used by tests and before export)."""
        if self._executor is not None:
            self._executor.submit(lambda: None).result(timeout)

    # -- conversation selection ------------------------------------------------------
    def current_conversation(self, scope: Any) -> int | None:
        if not isinstance(scope, Session):
            return None
        with self._lock:
            return self._conversation.get(scope.token_hash)

    def new_conversation(self, scope: Any) -> None:
        """The next message starts a new conversation (after CLEAR, sign-in, or 'new conversation')."""
        if isinstance(scope, Session):
            with self._lock:
                self._conversation.pop(scope.token_hash, None)

    def continue_conversation(self, scope: Session, conversation_id: int) -> None:
        """Make an earlier conversation (verified to belong to the user) the current one."""
        self._history.get_conversation(scope, conversation_id)  # raises if not the user's
        with self._lock:
            self._conversation[scope.token_hash] = int(conversation_id)

    def forget_session(self, scope: Any) -> None:
        if isinstance(scope, Session):
            with self._lock:
                self._conversation.pop(scope.token_hash, None)
                for seq in [s for s, t in self._turns.items() if t["scope"] is scope]:
                    self._turns.pop(seq, None)

    # -- event handlers (bus thread: only queue work) -----------------------------------
    def _submit(self, job, *args) -> None:
        if self._executor is None:
            return

        def run():
            try:
                job(*args)
            except AuthError as exc:
                log.info("History not saved: %s", exc.user_message)
            except Exception:
                log.exception("Saving conversation history failed")

        self._executor.submit(run)

    def _on_started(self, _name: str, payload: dict) -> None:
        scope = payload.get("scope")
        if not isinstance(scope, Session):
            return  # no signed-in user: nothing is stored
        seq = payload["seq"]
        with self._lock:
            original = self._turns.get(payload.get("retry_of")) if payload.get("retry_of") is not None else None
            key = original["key"] if original else f"{scope.token_hash[:16]}-{seq}"
            self._turns[seq] = {"scope": scope, "key": key, "message_id": None, "text": payload["text"]}
        self._submit(self._save_user, seq, scope, payload["text"], payload.get("source", "typed"), key)

    def _save_user(self, seq: int, scope: Session, text: str, source: str, key: str) -> None:
        conversation_id = self.current_conversation(scope)
        try:
            conversation_id, message_id = self._history.record_user_message(scope, conversation_id, text, source, key)
        except LookupError:  # the current conversation was deleted meanwhile: start a new one
            conversation_id, message_id = self._history.record_user_message(scope, None, text, source, key)
        with self._lock:
            self._conversation[scope.token_hash] = conversation_id
            if seq in self._turns:
                self._turns[seq]["message_id"] = message_id
        self._bus.publish(Events.HISTORY_CHANGED, {"conversation_id": conversation_id})

    def _turn(self, seq: int) -> dict | None:
        with self._lock:
            return self._turns.get(seq)

    def _on_ready(self, _name: str, payload: dict) -> None:
        turn = self._turn(payload["seq"])
        if turn is None:
            return
        self._submit(self._save_answer, payload["seq"], payload)

    def _save_answer(self, seq: int, payload: dict) -> None:
        turn = self._turn(seq)
        if turn is None or turn["message_id"] is None:
            return
        scope = turn["scope"]
        self._history.record_assistant_message(scope, turn["message_id"], payload["text"], payload.get("provider"),
                                               payload.get("model"), "completed")
        if payload.get("local"):
            for memory_id in payload.get("memory_ids") or []:
                try:
                    self._memory_assistant.link_memory_source(scope, memory_id, turn["message_id"])
                except Exception:
                    log.debug("Could not link memory source", exc_info=True)
        elif self._memory_assistant is not None:
            self._memory_assistant.auto_extract(scope, turn["text"], turn["message_id"])
        self._bus.publish(Events.HISTORY_CHANGED, {"conversation_id": self.current_conversation(scope)})

    def _on_failed(self, _name: str, payload: dict) -> None:
        if self._turn(payload["seq"]) is not None:
            self._submit(self._save_failure, payload["seq"], "failed", payload.get("kind"), payload.get("partial", ""))

    def _on_cancelled(self, _name: str, payload: dict) -> None:
        if self._turn(payload["seq"]) is not None:
            self._submit(self._save_failure, payload["seq"], "interrupted", "cancelled", payload.get("partial", ""))

    def _save_failure(self, seq: int, status: str, kind: str | None, partial: str) -> None:
        turn = self._turn(seq)
        if turn is None or turn["message_id"] is None:
            return
        scope = turn["scope"]
        if partial and partial.strip():
            # Keep what was really produced, clearly marked as incomplete (never as a completed answer).
            self._history.record_assistant_message(scope, turn["message_id"], partial, None, None, "interrupted")
        self._history.mark_user_message(scope, turn["message_id"], status, kind)
        self._bus.publish(Events.HISTORY_CHANGED, {"conversation_id": self.current_conversation(scope)})
