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
from bridge.protocol import (MAX_SCREEN_FPS, MAX_VOICE_SECONDS, VOICE_RATES, ProtocolError, b64, b64decode,
                             text_field)
from bridge.phone import PhoneState
from bridge.remote import RemoteControl, RemoteError

PHONE_EVENTS = {"status": Events.PHONE_STATUS, "notification": Events.PHONE_NOTIFICATION,
                "clipboard": Events.PHONE_CLIPBOARD, "file": Events.PHONE_FILE}
from services.schedule_service import ReminderService, reminder_speech

log = logging.getLogger(__name__)

SOURCE = "phone"
DELTA_EVERY_S = 0.15


class PhoneLink:
    def __init__(self, core, device, session, send: Callable[[dict], None],
                 clock: Callable[[], datetime] = datetime.now, poll_interval_s: float = 15.0,
                 remote: RemoteControl | None = None) -> None:
        self._core = core
        self._remote = remote or RemoteControl(core.desktop)
        self._screen_stop = threading.Event()
        self._screen_thread: threading.Thread | None = None
        self._screen_acked = threading.Event()
        self._last_notice = 0.0
        downloads = getattr(core, "phone_downloads", None) or (lambda: __import__("pathlib").Path.home() / "Downloads"
                                                                / "DUMBOO Mobile")
        self.phone = PhoneState(send, self._publish_phone, downloads)
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
        self._tts = ThreadPoolExecutor(max_workers=1, thread_name_prefix="PhoneTTS")
        self._input_ok_until = 0.0        # the account check for touchpad input is cached for a second
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
        elif kind == "agenda.do":
            self._require("agenda")
            self._agenda_do(msg)
        elif kind == "speak":
            self._require("voice")
            text = text_field(msg, "text")
            voice = getattr(self._core, "phone_voice", None)
            if voice is None:
                self._send({"t": "speech.unavailable", "id": msg.get("id")})
            else:
                self._tts.submit(voice.speak_to, self._send, msg.get("id"), text)
        elif kind in ("files.list", "files.get"):
            self._require("chat")
            self._files(kind, msg)
        elif kind.startswith("input.") or kind.startswith("screen."):
            self._control(kind, msg)
        elif kind == "phone.frame":
            self._phone_frame(msg)
        elif self.phone.handle(msg, self.device):
            pass
        else:
            raise ProtocolError("unknown_type", f"unknown message type: {kind}")

    def _require(self, capability: str) -> None:
        if not self.device.can(capability):
            raise ProtocolError("not_allowed", f"This phone isn't allowed to use “{capability}”. Change it on the "
                                               "laptop: ACCOUNT → Phone.")

    # -- what the phone shares (Phases 3-4) ----------------------------------------------------------------------
    def _publish_phone(self, kind: str, payload: dict) -> None:
        if kind == "clipboard":
            try:
                self._core.desktop.set_clipboard(payload["text"])   # the phone's clipboard, ready to paste
            except Exception:
                log.debug("Setting the laptop clipboard failed", exc_info=True)
        self._core.bus.publish(PHONE_EVENTS[kind], payload)
        if kind in ("file", "clipboard"):
            self._core.devices.audit(f"phone_{kind}", self.device.id, self.device.user_id,
                                     payload.get("file", "")[:100])

    def _phone_frame(self, msg: dict) -> None:
        if not self.phone.can("screen"):
            raise ProtocolError("not_allowed", "Screen sharing is switched off on the phone.")
        jpeg = b64decode(msg.get("img"), "img", 1_500_000)
        self._send({"t": "phone.frame.ack", "seq": msg.get("seq")})
        self._core.bus.publish(Events.PHONE_FRAME, {"device": self.device.id, "jpeg": jpeg, "seq": msg.get("seq"),
                                                    "w": int(msg.get("w") or 0), "h": int(msg.get("h") or 0)})

    # -- laptop control (Phase 2) ---------------------------------------------------------------------------------
    def update_device(self, device) -> None:
        """Capabilities changed on the laptop: apply them now and tell the phone."""
        self.device = device
        if not device.can("laptop_control"):
            self._stop_screen()
        self._send({"t": "caps", "caps": device.capabilities})

    def _control(self, kind: str, msg: dict) -> None:
        self._require("laptop_control")         # in memory: switching control off still applies at once
        now = time.monotonic()
        if kind not in ("input.move", "input.scroll", "screen.ack") or now > self._input_ok_until:
            self._core.auth.require(self.session, "automation.input")
            self._input_ok_until = now + 1.0
        try:
            if kind == "screen.start":
                self._start_screen(msg)
            elif kind == "screen.stop":
                self._stop_screen()
            elif kind == "screen.ack":
                self._screen_acked.set()
            elif kind == "input.click":
                self._remote.click(msg.get("x"), msg.get("y"), str(msg.get("button", "left")), bool(msg.get("double")))
                self._notice("mouse")
            elif kind == "input.move":
                self._remote.move(msg.get("dx"), msg.get("dy"))
            elif kind == "input.scroll":
                self._remote.scroll(msg.get("clicks"))
            elif kind == "input.key":
                self._remote.key(msg.get("combo"))
                self._notice("keyboard")
            elif kind == "input.text":
                self._remote.text(msg.get("text"))
                self._notice("keyboard")
            else:
                raise ProtocolError("unknown_type", f"unknown message type: {kind}")
        except RemoteError as exc:
            raise ProtocolError("bad_request", str(exc)) from exc

    def _notice(self, what: str, active: bool = True) -> None:
        """Tell the laptop's user (banner + log) that this phone is controlling it; at most every 20 s."""
        now = time.monotonic()
        if active and now - self._last_notice < 20:
            return
        self._last_notice = now if active else 0.0
        self._core.bus.publish(Events.PHONE_CONTROL, {"device": self.device.id, "name": self.device.name,
                                                      "what": what, "active": active})
        if active:
            self._core.devices.audit("remote_control", self.device.id, self.device.user_id, what)

    def _start_screen(self, msg: dict) -> None:
        fps = msg.get("fps", 5)
        width = msg.get("width", 1024)
        quality = msg.get("quality", 60)
        if not all(isinstance(v, (int, float)) for v in (fps, width, quality)):
            raise ProtocolError("bad_request", "fps, width and quality must be numbers")
        self._stop_screen()
        self._screen_stop.clear()
        self._screen_acked.set()
        interval = 1.0 / max(1.0, min(float(MAX_SCREEN_FPS), float(fps)))
        self._last_notice = 0.0
        self._notice("screen")

        def stream() -> None:
            seq = 0
            while not self._screen_stop.is_set():
                started = time.monotonic()
                # wait for the phone to confirm the last frame (at most 2 s), so a slow network never piles up frames
                self._screen_acked.wait(2.0)
                if self._screen_stop.is_set():
                    break
                self._screen_acked.clear()
                try:
                    jpeg, w, h = self._remote.frame(int(width), int(quality))
                except Exception as exc:
                    log.warning("Screen capture for the phone failed: %s", exc.__class__.__name__)
                    self._send({"t": "error", "code": "screen", "message": "The laptop screen couldn't be captured."})
                    break
                seq += 1
                self._send({"t": "screen.frame", "seq": seq, "w": w, "h": h, "img": b64(jpeg)})
                self._screen_stop.wait(max(0.0, interval - (time.monotonic() - started)))

        self._screen_thread = threading.Thread(target=stream, name="PhoneScreen", daemon=True)
        self._screen_thread.start()

    def _stop_screen(self) -> None:
        self._screen_stop.set()
        self._screen_acked.set()
        thread, self._screen_thread = self._screen_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
            self._notice("screen", active=False)

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

    def _agenda_do(self, msg: dict) -> None:
        """Add, complete or delete tasks and events from the phone's Agenda screen; answers with the new agenda."""
        from datetime import datetime as _dt

        action = msg.get("action")
        tasks, schedule, session = self._core.tasks, self._core.schedule, self.session
        try:
            if action == "add_task":
                due = msg.get("due") or None
                tasks.create(session, text_field(msg, "title"), priority=str(msg.get("priority") or "medium"),
                             due_date=due[:10] if due else None,
                             due_time=due[11:16] if due and len(due) >= 16 else None,
                             remind_at=msg.get("remind_at") or None)
            elif action == "complete_task":
                tasks.set_completed(session, int(msg["id"]), bool(msg.get("done", True)))
            elif action == "delete_task":
                tasks.delete(session, int(msg["id"]))
            elif action == "add_event":
                starts = _dt.fromisoformat(str(msg.get("starts_at")))
                minutes = msg.get("reminder_minutes", 10)
                schedule.create(session, text_field(msg, "title"), starts,
                                reminder_minutes=None if minutes is None else int(minutes))
            elif action == "delete_event":
                schedule.delete(session, int(msg["id"]))
            else:
                raise ProtocolError("bad_request", "unknown agenda action")
        except (KeyError, TypeError, ValueError) as exc:
            self._send({"t": "agenda.error", "message": str(exc) or "That didn't work."})
            return
        except ProtocolError:
            raise
        except Exception as exc:
            message = getattr(exc, "user_message", None) or str(exc)
            self._send({"t": "agenda.error", "message": message[:200]})
            return
        self._send(self.agenda())

    def _files(self, kind: str, msg: dict) -> None:
        """The phone's Files screen: files in the laptop's Downloads\\DUMBOO Mobile folder, and fetching one."""
        from bridge.phone import PhoneError, safe_filename

        folder = self.phone._downloads()
        if kind == "files.list":
            items = []
            if folder.is_dir():
                for p in sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
                    if p.is_file() and not p.name.endswith(".part"):
                        items.append({"name": p.name, "size": p.stat().st_size,
                                      "modified": int(p.stat().st_mtime)})
            self._send({"t": "files.list", "files": items})
            return
        name = safe_filename(msg.get("name"))
        path = (folder / name).resolve()
        if path.parent != folder.resolve() or not path.is_file():
            self._send({"t": "files.error", "message": "That file isn't in the laptop's DUMBOO Mobile folder."})
            return

        def send() -> None:
            try:
                self.phone.send_file(path, own=True)
            except PhoneError as exc:
                self._send({"t": "files.error", "message": exc.message})

        threading.Thread(target=send, name="PhoneFileOut", daemon=True).start()

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
        self.phone.close()
        self._stop_screen()
        self._reminders.stop()
        self.conversation.cancel("phone disconnected")
        self._stt.shutdown(wait=False, cancel_futures=True)
        self._tts.shutdown(wait=False, cancel_futures=True)
        try:
            self._core.auth.logout(self.session, "phone disconnected")
        except Exception:
            log.debug("Phone session logout failed", exc_info=True)
