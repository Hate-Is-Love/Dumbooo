"""Message types, limits and the signed-challenge format of the phone link (docs/PHONE_BRIDGE.md)."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

VERSION = 1
MAX_FRAME_BYTES = 2 * 1024 * 1024
MAX_VOICE_SECONDS = 20
VOICE_RATES = (16000,)
AUTH_TIMEOUT_S = 10.0
RATE_WINDOW_S = 10.0
RATE_MAX_MESSAGES = 20
INPUT_RATE_MAX = 400          # touchpad moves, clicks, keys and screen acks: per RATE_WINDOW_S
INPUT_TYPES = frozenset({"input.click", "input.move", "input.scroll", "input.key", "input.text", "screen.ack",
                         "phone.frame", "file.chunk", "phone.notification", "phone.status", "phone.result"})
MAX_SCREEN_FPS = 8
MAX_TEXT = 2000
MAX_NAME = 60

DEFAULT_CAPABILITIES = {"chat": True, "voice": True, "agenda": True, "notifications": True,
                        "laptop_control": False}


class ProtocolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def challenge_message(nonce: bytes, server_fingerprint: str) -> bytes:
    """What the phone signs: binds the challenge to this laptop's certificate."""
    return b"DUMBOO-AUTH-v1|" + nonce + b"|" + server_fingerprint.encode("ascii")


def b64decode(value: Any, field: str, max_bytes: int) -> bytes:
    if not isinstance(value, str) or len(value) > max_bytes * 4 // 3 + 8:
        raise ProtocolError("bad_request", f"{field} is missing or too large")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProtocolError("bad_request", f"{field} is not valid base64") from exc


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def parse(frame: str | bytes) -> dict:
    if isinstance(frame, bytes):
        raise ProtocolError("bad_request", "binary frames are not used")
    try:
        msg = json.loads(frame)
    except ValueError as exc:
        raise ProtocolError("bad_request", "not JSON") from exc
    if not isinstance(msg, dict) or not isinstance(msg.get("t"), str):
        raise ProtocolError("bad_request", "every message needs a type")
    return msg


def text_field(msg: dict, field: str, limit: int = MAX_TEXT) -> str:
    value = msg.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError("bad_request", f"{field} is required")
    if len(value) > limit:
        raise ProtocolError("bad_request", f"{field} is too long")
    return value.strip()


def dump(msg: dict) -> str:
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":"), default=str)
