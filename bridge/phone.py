"""Laptop → phone (DUMBOO Mobile Phases 3–4): commands to the phone, and what the phone shares with the laptop.

The phone decides what the laptop may do to it: each feature (notifications, clipboard, files, calls, music,
find my phone, opening apps, screen sharing) is switched on in the app's settings and backed by Android's own
permission on the phone. The phone tells the laptop which features are on (``phone.features``); the laptop only
sends commands for those, and the phone checks again before acting.

Commands: ``{"t":"phone.cmd","id":7,"action":"call","args":{"who":"Mom"}}`` →
``{"t":"phone.result","id":7,"ok":true,"message":"Calling Mom…","data":{...}}``.
"""

from __future__ import annotations

import itertools
import logging
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# action -> phone feature that must be on
ACTIONS = {
    "call": "calls", "dial": "calls",
    "play": "music", "media": "music", "volume": "music",
    "ring": "find_phone", "stop_ring": "find_phone",
    "open_app": "open_apps",
    "clipboard.set": "clipboard",
    "reply": "notifications", "dismiss": "notifications",
    "screen.start": "screen", "screen.stop": "screen",
    "tap": "screen", "swipe": "screen", "global": "screen", "type": "screen",
    "torch": "open_apps", "alarm": "open_apps", "timer": "open_apps", "navigate": "open_apps", "search": "open_apps",
    "sms": "calls",
    "status": None,
}
# Commands the owner speaks or types INTO the phone, for the phone itself ("call mom", "set an alarm for 7"). The
# "what the laptop may do" switches limit the laptop, not the owner, so these skip them; the phone still checks that
# its owner sent a message within the last 90 seconds, and Android's own permissions still apply.
OWN_ACTIONS = frozenset({"call", "dial", "play", "media", "volume", "open_app", "torch", "alarm", "timer", "navigate",
                         "search", "sms"})
MEDIA_KEYS = ("play", "pause", "playpause", "next", "previous", "stop")
GLOBAL_ACTIONS = ("back", "home", "recents", "notifications", "quick_settings", "lock")
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_RECEIVED_PER_CONNECTION = 1024 * 1024 * 1024      # a paired phone can't fill the laptop's disk
CHUNK_BYTES = 512 * 1024
SAFE_NAME = re.compile(r"[^\w .()\[\]-]+")


class PhoneError(RuntimeError):
    def __init__(self, message: str, data: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.data = data or {}


def safe_filename(name: str) -> str:
    base = Path(str(name or "file")).name
    base = SAFE_NAME.sub("_", base).strip(" .") or "file"
    return base[:120]


def unique_path(folder: Path, name: str) -> Path:
    path = folder / name
    stem, suffix = path.stem, path.suffix
    for n in itertools.count(2):
        if not path.exists():
            return path
        path = folder / f"{stem} ({n}){suffix}"
    return path  # pragma: no cover


class PhoneState:
    """What the connected phone has shared: features, status, recent notifications, incoming files."""

    def __init__(self, send: Callable[[dict], None], publish: Callable[[str, dict], None],
                 downloads: Callable[[], Path]) -> None:
        self._send = send
        self._publish = publish
        self._downloads = downloads
        self.features: dict[str, bool] = {}
        self.status: dict = {}
        self.notifications: deque[dict] = deque(maxlen=50)
        self._ids = itertools.count(1)
        self._waiting: dict[int, tuple[threading.Event, dict]] = {}
        self._incoming: dict[str, dict] = {}
        self._received = 0
        self._lock = threading.Lock()

    # -- commands ------------------------------------------------------------------------------------------------
    def can(self, feature: str | None) -> bool:
        return feature is None or bool(self.features.get(feature))

    def request(self, action: str, args: dict | None = None, timeout: float = 12.0, own: bool = False) -> dict:
        if action not in ACTIONS:
            raise PhoneError(f"Unknown phone action: {action}")
        feature = ACTIONS[action]
        own = own and action in OWN_ACTIONS
        if own:
            args = {**(args or {}), "own": True}
        elif not self.can(feature):
            raise PhoneError(f"“{feature.replace('_', ' ')}” is switched off on your phone. Turn it on in DUMBOO "
                             "Mobile → Settings → What the laptop may do.")
        cmd_id = next(self._ids)
        event, box = threading.Event(), {}
        with self._lock:
            self._waiting[cmd_id] = (event, box)
        self._send({"t": "phone.cmd", "id": cmd_id, "action": action, "args": args or {}})
        if not event.wait(timeout):
            with self._lock:
                self._waiting.pop(cmd_id, None)
            raise PhoneError("The phone didn't answer in time. Is DUMBOO Mobile still open and connected?")
        if not box.get("ok"):
            raise PhoneError(box.get("message") or "The phone couldn't do that.", box.get("data"))
        return box

    def send_nowait(self, action: str, args: dict | None = None) -> None:
        """Fire-and-forget (taps and swipes on the phone screen)."""
        if not self.can(ACTIONS.get(action)):
            raise PhoneError("Screen control is switched off on your phone.")
        self._send({"t": "phone.cmd", "id": 0, "action": action, "args": args or {}})

    # -- messages from the phone ------------------------------------------------------------------------------------
    def handle(self, msg: dict, device) -> bool:
        kind = msg["t"]
        if kind == "phone.result":
            with self._lock:
                waiter = self._waiting.pop(msg.get("id"), None)
            if waiter is not None:
                waiter[1].update(ok=bool(msg.get("ok")), message=str(msg.get("message") or ""),
                                 data=msg.get("data") if isinstance(msg.get("data"), dict) else {})
                waiter[0].set()
        elif kind == "phone.features":
            features = msg.get("features")
            if isinstance(features, dict):
                self.features = {str(k): bool(v) for k, v in features.items() if k in set(ACTIONS.values()) | {
                    "notifications", "files"}}
            self._publish_status(device)
        elif kind == "phone.status":
            status = msg.get("status")
            if isinstance(status, dict):
                self.status = {k: status[k] for k in ("battery", "charging", "network", "ringer", "model")
                               if k in status}
            self._publish_status(device)
        elif kind == "phone.notification":
            self._notification(msg, device)
        elif kind == "clip.push":
            text = msg.get("text")
            # "own": the owner pressed "send clipboard" on the phone themselves
            if isinstance(text, str) and text and len(text) <= 20000 and (self.can("clipboard") or msg.get("own")):
                self._publish("clipboard", {"device": device.id, "name": device.name, "text": text})
        elif kind in ("file.begin", "file.chunk", "file.end"):
            self._file(kind, msg, device)
        else:
            return False
        return True

    def _publish_status(self, device) -> None:
        self._publish("status", {"device": device.id, "name": device.name, "features": dict(self.features),
                                 "status": dict(self.status)})

    def _notification(self, msg: dict, device) -> None:
        if not self.can("notifications"):
            return
        note = {k: str(msg.get(k) or "")[:500] for k in ("key", "app", "title", "text")}
        note["can_reply"] = bool(msg.get("can_reply"))
        note["removed"] = bool(msg.get("removed"))
        note["at"] = time.strftime("%H:%M")
        if not note["key"]:
            return
        with self._lock:
            for old in list(self.notifications):
                if old["key"] == note["key"]:
                    self.notifications.remove(old)
            if not note["removed"]:
                self.notifications.appendleft(note)
        self._publish("notification", {"device": device.id, "name": device.name, **note})

    def find_notification(self, who: str, replyable: bool = False) -> dict | None:
        words = [w for w in re.findall(r"[a-z0-9]+", who.lower()) if w not in ("the", "my", "on", "from")]
        with self._lock:
            notes = list(self.notifications)
        for note in notes:
            hay = f"{note['title']} {note['app']}".lower()
            if words and all(w in hay for w in words) and (note["can_reply"] or not replyable):
                return note
        return None

    # -- files from the phone ------------------------------------------------------------------------------------------
    def _file(self, kind: str, msg: dict, device) -> None:
        from bridge.protocol import ProtocolError, b64decode

        file_id = str(msg.get("id") or "")[:40]
        if kind == "file.begin" and not (self.can("files") or msg.get("own")):
            raise ProtocolError("not_allowed", "Sending files is switched off on the phone.")
        if kind == "file.begin":
            size = msg.get("size")
            if not isinstance(size, int) or not 0 < size <= MAX_FILE_BYTES:
                raise ProtocolError("bad_request", f"Files must be 1 byte to {MAX_FILE_BYTES // 1024 // 1024} MB.")
            if self._received + size > MAX_RECEIVED_PER_CONNECTION or len(self._incoming) >= 4:
                raise ProtocolError("rate_limit", "Too many files at once; reconnect to send more.")
            self._received += size
            folder = self._downloads()
            folder.mkdir(parents=True, exist_ok=True)
            final = unique_path(folder, safe_filename(msg.get("name")))
            part = final.with_name(final.name + ".part")
            self._incoming[file_id] = {"final": final, "part": part, "size": size, "got": 0,
                                       "handle": part.open("wb")}
            return
        entry = self._incoming.get(file_id)
        if entry is None:
            raise ProtocolError("bad_request", "unknown file transfer")
        if kind == "file.chunk":
            data = b64decode(msg.get("data"), "data", CHUNK_BYTES)
            entry["got"] += len(data)
            if entry["got"] > entry["size"]:
                self._abort(file_id)
                raise ProtocolError("bad_request", "the file is bigger than announced")
            entry["handle"].write(data)
            return
        entry["handle"].close()
        self._incoming.pop(file_id, None)
        if entry["got"] != entry["size"]:
            entry["part"].unlink(missing_ok=True)
            raise ProtocolError("bad_request", "the file arrived incomplete")
        entry["part"].replace(entry["final"])
        self._send({"t": "file.saved", "id": file_id, "name": entry["final"].name})
        self._publish("file", {"device": device.id, "name": device.name, "file": entry["final"].name,
                               "path": str(entry["final"]), "size": entry["size"]})

    def _abort(self, file_id: str) -> None:
        entry = self._incoming.pop(file_id, None)
        if entry is not None:
            entry["handle"].close()
            entry["part"].unlink(missing_ok=True)

    def close(self) -> None:
        for file_id in list(self._incoming):
            self._abort(file_id)
        with self._lock:
            waiting = list(self._waiting.values())
            self._waiting.clear()
        for event, box in waiting:
            box.update(ok=False, message="The phone disconnected.")
            event.set()

    # -- files to the phone ------------------------------------------------------------------------------------------
    def send_file(self, path: Path, timeout: float = 120.0, own: bool = False) -> dict:
        """``own``: the phone asked for this file itself (its Files screen), so its "files" switch isn't needed."""
        import base64
        import mimetypes

        if not own and not self.can("files"):
            raise PhoneError("Receiving files is switched off on your phone.")
        path = Path(path)
        size = path.stat().st_size
        if not 0 < size <= MAX_FILE_BYTES:
            raise PhoneError(f"Files must be up to {MAX_FILE_BYTES // 1024 // 1024} MB.")
        file_id = f"L{next(self._ids)}"
        self._send({"t": "file.begin", "id": file_id, "name": safe_filename(path.name), "size": size,
                    "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                    "requested": own})
        with path.open("rb") as handle:
            while chunk := handle.read(CHUNK_BYTES):
                self._send({"t": "file.chunk", "id": file_id, "data": base64.b64encode(chunk).decode()})
        return self.request_end(file_id, timeout)

    def request_end(self, file_id: str, timeout: float) -> dict:
        cmd_id = next(self._ids)
        event, box = threading.Event(), {}
        with self._lock:
            self._waiting[cmd_id] = (event, box)
        self._send({"t": "file.end", "id": file_id, "reply": cmd_id})
        if not event.wait(timeout):
            raise PhoneError("The phone didn't confirm the file.")
        if not box.get("ok"):
            raise PhoneError(box.get("message") or "The phone couldn't save the file.")
        return box
