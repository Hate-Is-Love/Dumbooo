"""One connected phone: its own DUMBOO conversation, push-to-talk transcription, agenda and reminders.

The phone gets a separate ``ConversationController`` on a private event bus, with the same hook chain as the laptop
(tasks, everyday, research, memory, AI) and its own session for the paired user. So the phone never interrupts the
laptop's conversation, and the laptop's dashboard and voice don't react to the phone's messages.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable

import numpy as np

from ai.controller import ConversationController
from app.event_bus import EventBus, Events
from bridge.protocol import MAX_VOICE_SECONDS, VOICE_RATES, ProtocolError, b64decode, text_field
from services.schedule_service import ReminderService, reminder_speech

log = logging.getLogger(__name__)

SOURCE = "phone"
DELTA_EVERY_S = 0.15


class PhoneLink:
    def __init__(self, core, device, session, send: Callable[[dict], None],
                 clock: Callable[[], datetime] = datetime.now, poll_interval_s: float = 15.0) -> None:
        self._core = core
        self.device = device
        self.session = session
        self._send = send
        self._clock = clock
        self._bus = EventBus(synchronous=True)
        kwargs = {"provider_factory": core.ai_provider_factory} if getattr(core, "ai_provider_factory", None) else {}
        self.conversation = ConversationController(self._bus, core.ai_config(), core.paths.root,
                                                   voice_output=lambda: False, hooks=core.guardian_assistant, **kwargs)
        self._seq = 0
        self._ids: dict[int, object] = {}     # conversation seq -> the phone's message id
        self._last_delta = 0.0
        self._lock = threading.Lock()
        self._stt = ThreadPoolExecutor(max_workers=1, thread_name_prefix="PhoneSTT")
        for name, handler in ((Events.RESPONSE_DELTA, self._on_delta), (Events.RESPONSE_READY, self._on_ready),
                              (Events.RESPONSE_FAILED, self._on_failed),
                              (Events.RESPONSE_CANCELLED, self._on_cancelled),
                              (Events.REMINDER_DUE, lambda _n, p: self.push_reminder(p))):
            self._bus.subscribe(name, handler)
        # Reminders while the laptop isn't signed in as this phone's user (otherwise the laptop's own reminder
        # service delivers them and the bridge forwards them, so nothing is announced twice).
        self._reminders = ReminderService(core.schedule, self._bus, self._poll_session, interval_s=poll_interval_s,
                                          tasks=core.tasks)
        self._reminders.start()

    # -- incoming ---------------------------------------------------------------------------------------------
    def handle(self, msg: dict) -> None:
        kind = msg["t"]
        if kind == "ping":
            self._send({"t": "pong"})
        elif kind == "chat":
            self._require("chat")
            self.ask(text_field(msg, "text"), msg.get("id"))
        elif kind == "voice":
            self._require("voice")
            self._voice(msg)
        elif kind == "agenda.get":
            self._require("agenda")
            self._send(self.agenda())
        else:
            raise ProtocolError("unknown_type", f"unknown message type: {kind}")

    def _require(self, capability: str) -> None:
        if not self.device.can(capability):
            raise ProtocolError("not_allowed", f"This phone isn't allowed to use “{capability}”. Change it on the "
                                               "laptop: ACCOUNT → Phone.")

    def ask(self, text: str, message_id) -> None:
        with self._lock:
            self._seq += 1
            seq = self._seq
            self._ids[seq] = message_id
        if not self.conversation.submit(text, SOURCE, seq, scope=self.session):
            with self._lock:
                self._ids.pop(seq, None)
            self._send({"t": "answer.error", "id": message_id, "message": "That message was empty or a repeat."})

    def _voice(self, msg: dict) -> None:
        rate = msg.get("rate")
        if rate not in VOICE_RATES:
            raise ProtocolError("bad_request", "voice must be 16 kHz mono 16-bit PCM")
        pcm = b64decode(msg.get("pcm"), "pcm", MAX_VOICE_SECONDS * rate * 2)
        if len(pcm) < rate // 5 * 2:   # under 0.2 s
            raise ProtocolError("bad_request", "the recording is too short")
        message_id = msg.get("id")
        self._stt.submit(self._transcribe, pcm, rate, message_id)

    def _transcribe(self, pcm: bytes, rate: int, message_id) -> None:
        from audio.speech_to_text import transcribe_array

        stt = self._core.stt
        try:
            audio = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
            model = self._core.whisper_pool.get(stt.model_name, stt.device, stt.compute_type)
            result = transcribe_array(model, audio, stt.language)
        except Exception:
            log.exception("Phone voice transcription failed")
            self._send({"t": "answer.error", "id": message_id, "message": "Speech recognition failed on the laptop."})
            return
        if result.empty:
            self._send({"t": "answer.error", "id": message_id, "message": "I couldn't make out any words."})
            return
        self._send({"t": "transcript", "id": message_id, "text": result.text})
        self.ask(result.text, message_id)

    # -- answers -------------------------------------------------------------------------------------------------
    def _id_for(self, payload: dict, pop: bool = False):
        with self._lock:
            return self._ids.pop(payload.get("seq"), None) if pop else self._ids.get(payload.get("seq"))

    def _on_delta(self, _name: str, payload: dict) -> None:
        now = time.monotonic()
        if now - self._last_delta < DELTA_EVERY_S:
            return
        self._last_delta = now
        self._send({"t": "answer.delta", "id": self._id_for(payload), "text": payload.get("text", "")})

    def _on_ready(self, _name: str, payload: dict) -> None:
        self._send({"t": "answer", "id": self._id_for(payload, pop=True), "text": payload.get("text", ""),
                    "speech": payload.get("speech", ""), "kind": payload.get("model", ""),
                    "listen_after": bool(payload.get("listen_after"))})

    def _on_failed(self, _name: str, payload: dict) -> None:
        self._send({"t": "answer.error", "id": self._id_for(payload, pop=True),
                    "message": payload.get("error") or "The request failed."})

    def _on_cancelled(self, _name: str, payload: dict) -> None:
        message_id = self._id_for(payload, pop=True)
        if message_id is not None:
            self._send({"t": "answer.error", "id": message_id, "message": "Cancelled by a newer message."})

    # -- agenda and reminders ------------------------------------------------------------------------------------
    def agenda(self) -> dict:
        tasks = self._core.tasks.list(self.session, False)
        events = self._core.schedule.upcoming(self.session, days=7, include_today=True)
        now = self._clock()
        return {"t": "agenda",
                "tasks": [{"id": t.id, "title": t.title, "priority": t.priority,
                           "due": t.due_at.isoformat(timespec="minutes") if t.due_at else None,
                           "has_time": t.due_time is not None, "overdue": t.is_overdue(now),
                           "remind_at": t.remind_at.isoformat(timespec="minutes") if t.remind_at else None}
                          for t in tasks[:100]],
                "events": [{"id": e.id, "title": e.title, "starts_at": e.starts_at.isoformat(timespec="minutes"),
                            "reminder": e.reminder_minutes == 0 and e.description == "Set by DUMBOO"}
                           for e in events if e.starts_at >= now][:100]}

    def push_reminder(self, payload: dict) -> None:
        if not self.device.can("notifications"):
            return
        self._send({"t": "reminder", "title": payload.get("title", "Reminder"),
                    "text": reminder_speech(payload, self._clock()), "kind": payload.get("kind", "event"),
                    "at": payload.get("starts_at"), "missed": bool(payload.get("missed"))})

    def laptop_has_this_user(self) -> bool:
        laptop = self._core.accounts.session
        if laptop is None:
            return False
        try:
            return self._core.auth.require(laptop).id == self.device.user_id
        except Exception:
            return False

    def _poll_session(self):
        return None if self.laptop_has_this_user() else self.session

    # -- end ---------------------------------------------------------------------------------------------------
    def close(self) -> None:
        self._reminders.stop()
        self.conversation.cancel("phone disconnected")
        self._stt.shutdown(wait=False, cancel_futures=True)
        try:
            self._core.auth.logout(self.session, "phone disconnected")
        except Exception:
            log.debug("Phone session logout failed", exc_info=True)
