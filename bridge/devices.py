"""Paired phones: one-time pairing codes, public-key checks, capabilities, removal and the audit log.

A phone is bound to the DUMBOO user who paired it. It proves who it is on every connection by signing the
laptop's random challenge with the private key that never leaves its Android Keystore.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from bridge.protocol import DEFAULT_CAPABILITIES, MAX_NAME, ProtocolError, challenge_message

log = logging.getLogger(__name__)

CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"   # 32 symbols, no 0/O or 1/I look-alikes: 8 symbols = 40 bits
PAIRING_WINDOW_S = 120.0
MAX_CODE_TRIES = 5
MAX_DEVICES_PER_USER = 5


@dataclass
class Device:
    id: int
    user_id: int
    name: str
    public_key: bytes
    capabilities: dict
    created_at: str
    last_seen_at: str | None = None

    def can(self, capability: str) -> bool:
        return bool(self.capabilities.get(capability, False))


@dataclass
class _Window:
    user_id: int
    code_hash: bytes
    expires: float
    tries: int = 0
    used: bool = False


@dataclass
class PairingCode:
    code: str            # shown as ABCD-EFGH
    expires_in_s: int


def _hash_code(code: str) -> bytes:
    return hashlib.sha256(code.replace("-", "").strip().upper().encode()).digest()


def key_fingerprint(public_key_der: bytes) -> str:
    return hashlib.sha256(public_key_der).hexdigest()


def load_public_key(der: bytes) -> ec.EllipticCurvePublicKey:
    try:
        key = serialization.load_der_public_key(der)
    except (ValueError, TypeError) as exc:
        raise ProtocolError("bad_key", "the phone's public key could not be read") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ProtocolError("bad_key", "only EC P-256 keys are accepted")
    return key


def signature_ok(public_key_der: bytes, signature: bytes, nonce: bytes, server_fingerprint: str) -> bool:
    try:
        load_public_key(public_key_der).verify(signature, challenge_message(nonce, server_fingerprint),
                                               ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError):
        return False


class DeviceRegistry:
    def __init__(self, db, auth, clock: Callable[[], datetime] = datetime.now,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._db = db
        self._auth = auth
        self._clock = clock
        self._mono = monotonic
        self._windows: list[_Window] = []
        self._lock = threading.Lock()
        self.on_revoked: Callable[[int], None] = lambda device_id: None   # the server closes that phone's link

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    # -- audit ------------------------------------------------------------------------------------------------
    def audit(self, event: str, device_id: int | None = None, user_id: int | None = None, detail: str = "") -> None:
        self._db.execute("INSERT INTO device_events (device_id, user_id, event, detail, at) VALUES (?, ?, ?, ?, ?)",
                         (device_id, user_id, event[:40], detail[:300], self._now()))

    def events(self, session, limit: int = 50) -> list[dict]:
        user = self._auth.require(session)
        return self._db.query("SELECT e.*, d.name AS device_name FROM device_events e LEFT JOIN paired_devices d "
                              "ON d.id = e.device_id WHERE e.user_id = ? ORDER BY e.id DESC LIMIT ?",
                              (user.id, int(limit)))

    # -- pairing ------------------------------------------------------------------------------------------------
    def start_pairing(self, session) -> PairingCode:
        """Open a 2-minute pairing window for the signed-in user and return its one-time code."""
        user = self._auth.require(session)
        active = self._db.row("SELECT COUNT(*) AS n FROM paired_devices WHERE user_id = ? AND revoked_at IS NULL",
                              (user.id,))["n"]
        if active >= MAX_DEVICES_PER_USER:
            raise ProtocolError("too_many", f"You already have {active} phones paired. Remove one first.")
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        with self._lock:
            self._windows = [w for w in self._windows if w.user_id != user.id and w.expires > self._mono()]
            self._windows.append(_Window(user.id, _hash_code(raw), self._mono() + PAIRING_WINDOW_S))
        self.audit("pairing_opened", user_id=user.id)
        return PairingCode(f"{raw[:4]}-{raw[4:]}", int(PAIRING_WINDOW_S))

    def cancel_pairing(self, session) -> None:
        user = self._auth.require(session)
        with self._lock:
            self._windows = [w for w in self._windows if w.user_id != user.id]

    def pairing_open(self) -> bool:
        with self._lock:
            return any(not w.used and w.expires > self._mono() for w in self._windows)

    def _claim(self, code: str) -> _Window:
        wanted = _hash_code(code)
        with self._lock:
            now = self._mono()
            self._windows = [w for w in self._windows if w.expires > now and not w.used]
            for w in self._windows:
                if hmac.compare_digest(w.code_hash, wanted):
                    w.used = True
                    return w
            for w in self._windows:                        # a wrong code costs every open window a try
                w.tries += 1
            closed = [w for w in self._windows if w.tries >= MAX_CODE_TRIES]
            self._windows = [w for w in self._windows if w.tries < MAX_CODE_TRIES]
        for w in closed:
            self.audit("pairing_locked", user_id=w.user_id, detail="too many wrong codes")
        raise ProtocolError("bad_code", "That pairing code is wrong or has expired. Show a new QR code on the laptop.")

    def pair(self, code: str, name: str, public_key_der: bytes, signature: bytes, nonce: bytes,
             server_fingerprint: str) -> Device:
        if not self.pairing_open():
            raise ProtocolError("not_pairing", "Pairing isn't open. Click “Pair a phone” on the laptop first.")
        load_public_key(public_key_der)
        if not signature_ok(public_key_der, signature, nonce, server_fingerprint):
            raise ProtocolError("bad_signature", "The phone couldn't prove it holds its key.")
        window = self._claim(code)
        name = " ".join(str(name or "Phone").split())[:MAX_NAME] or "Phone"
        fp = key_fingerprint(public_key_der)
        now = self._now()
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM paired_devices WHERE key_fingerprint = ? AND revoked_at IS NOT NULL", (fp,))
            if conn.execute("SELECT 1 FROM paired_devices WHERE key_fingerprint = ?", (fp,)).fetchone():
                raise ProtocolError("already_paired", "This phone is already paired.")
            cur = conn.execute(
                "INSERT INTO paired_devices (user_id, name, public_key, key_fingerprint, capabilities, created_at, "
                "last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (window.user_id, name, public_key_der.hex(), fp, json.dumps(DEFAULT_CAPABILITIES), now, now))
            device_id = cur.lastrowid
        self.audit("paired", device_id, window.user_id, name)
        log.info("Phone paired (device %s)", device_id)
        return self.get(device_id)

    # -- authentication ----------------------------------------------------------------------------------------------
    def get(self, device_id: int) -> Device | None:
        row = self._db.row("SELECT * FROM paired_devices WHERE id = ? AND revoked_at IS NULL", (int(device_id),))
        if row is None:
            return None
        return Device(row["id"], row["user_id"], row["name"], bytes.fromhex(row["public_key"]),
                      {**DEFAULT_CAPABILITIES, **json.loads(row["capabilities"] or "{}")}, row["created_at"],
                      row["last_seen_at"])

    def authenticate(self, device_id, signature: bytes, nonce: bytes, server_fingerprint: str) -> Device:
        if not isinstance(device_id, int):
            raise ProtocolError("bad_request", "device id is required")
        device = self.get(device_id)
        if device is None:
            raise ProtocolError("unknown_device", "This phone isn't paired (or was removed). Pair it again.")
        if not signature_ok(device.public_key, signature, nonce, server_fingerprint):
            self.audit("auth_failed", device.id, device.user_id, "bad signature")
            raise ProtocolError("bad_signature", "The phone's signature didn't match. Pair it again.")
        self._db.execute("UPDATE paired_devices SET last_seen_at = ? WHERE id = ?", (self._now(), device.id))
        return device

    # -- management ---------------------------------------------------------------------------------------------------
    def list(self, session) -> list[dict]:
        user = self._auth.require(session)
        rows = self._db.query("SELECT id, name, capabilities, created_at, last_seen_at FROM paired_devices "
                              "WHERE user_id = ? AND revoked_at IS NULL ORDER BY created_at", (user.id,))
        for r in rows:
            r["capabilities"] = {**DEFAULT_CAPABILITIES, **json.loads(r["capabilities"] or "{}")}
        return rows

    def revoke(self, session, device_id: int) -> bool:
        user = self._auth.require(session)
        changed = self._db.execute("UPDATE paired_devices SET revoked_at = ? WHERE id = ? AND user_id = ? AND "
                                   "revoked_at IS NULL", (self._now(), int(device_id), user.id))
        if changed:
            self.audit("removed", int(device_id), user.id)
            self.on_revoked(int(device_id))
        return bool(changed)

    def set_capability(self, session, device_id: int, capability: str, allowed: bool) -> dict:
        user = self._auth.require(session)
        if capability not in DEFAULT_CAPABILITIES:
            raise ProtocolError("bad_request", "unknown capability")
        row = self._db.row("SELECT capabilities FROM paired_devices WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
                           (int(device_id), user.id))
        if row is None:
            raise ProtocolError("unknown_device", "That phone isn't paired.")
        caps = {**DEFAULT_CAPABILITIES, **json.loads(row["capabilities"] or "{}"), capability: bool(allowed)}
        self._db.execute("UPDATE paired_devices SET capabilities = ? WHERE id = ?", (json.dumps(caps), int(device_id)))
        self.audit("capability", int(device_id), user.id, f"{capability}={'on' if allowed else 'off'}")
        return caps


__all__ = ["DeviceRegistry", "Device", "PairingCode", "key_fingerprint", "signature_ok", "field"]
