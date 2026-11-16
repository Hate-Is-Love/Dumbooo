"""DUMBOO Mobile bridge: pairing, authentication, the phone's conversation, voice, agenda, reminders and the safety
gates, over real TLS WebSocket connections in-process (acting like the Android app; no phone needed)."""

import base64
import hashlib
import json
import socket
import ssl
import time

import numpy as np
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from websockets.sync.client import connect

from app.event_bus import Events
from bridge.protocol import challenge_message
from tests.fakes import wait_for
from tests.test_history_memory import Rig


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def rig(paths):
    r = Rig(paths, bridge_overrides={"identity": {"protect": lambda b: b, "unprotect": lambda b: b},
                                     "bridge": {"addresses": lambda: ["127.0.0.1"], "host": "127.0.0.1"}})
    r.core.settings.update({"bridge": {"port": free_port()}})
    yield r
    r.core.bridge.stop()
    r.close()


class Phone:
    """Behaves like the Android app: pins the laptop certificate and signs the challenge with its own key."""

    def __init__(self, key=None):
        self.key = key or ec.generate_private_key(ec.SECP256R1())
        self.pub = self.key.public_key().public_bytes(serialization.Encoding.DER,
                                                      serialization.PublicFormat.SubjectPublicKeyInfo)
        self.ws = None
        self.nonce = b""
        self.pinned = ""

    def connect(self, port: int, fingerprint: str):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE       # trust comes from the pin below, like the app
        self.ws = connect(f"wss://127.0.0.1:{port}", ssl=ctx, open_timeout=15, compression=None)
        der = self.ws.socket.getpeercert(binary_form=True)
        self.pinned = hashlib.sha256(der).hexdigest()
        assert self.pinned == fingerprint, "certificate pin mismatch"
        hello = self.recv()
        assert hello["t"] == "hello" and hello["v"] == 1
        self.nonce = base64.b64decode(hello["nonce"])
        return self

    def sign(self, nonce=None, fingerprint=None) -> str:
        msg = challenge_message(nonce or self.nonce, fingerprint or self.pinned)
        return base64.b64encode(self.key.sign(msg, ec.ECDSA(hashes.SHA256()))).decode()

    def send(self, msg: dict):
        self.ws.send(json.dumps(msg))

    def recv(self, timeout=5) -> dict:
        return json.loads(self.ws.recv(timeout=timeout))

    def until(self, kind: str, timeout=8) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            msg = self.recv(timeout=max(0.1, end - time.monotonic()))
            if msg["t"] == kind:
                return msg
        raise AssertionError(f"no {kind}")

    def pair(self, code: str, name="Test phone") -> dict:
        self.send({"t": "pair", "code": code, "name": name, "pub": base64.b64encode(self.pub).decode(),
                   "sig": self.sign()})
        return self.recv()

    def auth(self, device_id: int) -> dict:
        self.send({"t": "auth", "device": device_id, "sig": self.sign()})
        return self.recv()

    def close(self):
        if self.ws is not None:
            self.ws.close()


def paired(rig, name="Test phone"):
    offer = rig.core.bridge.offer_pairing(rig.session)
    phone = Phone().connect(offer.port, offer.fingerprint)
    ready = phone.pair(offer.code, name)
    assert ready["t"] == "ready", ready
    return phone, ready, offer


# -- pairing and authentication --------------------------------------------------------------------------------------
def test_pairing_by_qr_code_then_reconnecting_with_the_phone_key(rig):
    offer = rig.core.bridge.offer_pairing(rig.session)
    assert offer.uri.startswith("dumboo://pair?d=")
    data = json.loads(base64.urlsafe_b64decode(offer.uri.split("d=")[1] + "=="))
    assert data["c"] == offer.code and data["fp"] == offer.fingerprint and data["p"] == offer.port
    assert data["h"] == ["127.0.0.1"] and len(offer.code) == 9
    phone = Phone().connect(offer.port, offer.fingerprint)
    ready = phone.pair(offer.code, "Pixel 8")
    assert ready["t"] == "ready" and ready["caps"]["chat"] and not ready["caps"]["laptop_control"]
    phone.close()
    [device] = rig.core.devices.list(rig.session)
    assert device["name"] == "Pixel 8"
    again = Phone(phone.key).connect(offer.port, offer.fingerprint)   # same key, new connection: no code needed
    assert again.auth(ready["device"])["t"] == "ready"
    again.close()
    events = [e["event"] for e in rig.core.devices.events(rig.session)]
    assert {"pairing_opened", "paired", "connected"} <= set(events)


def test_a_code_works_once_and_wrong_codes_close_pairing(rig):
    phone, ready, offer = paired(rig)
    phone.close()
    reuse = Phone().connect(offer.port, offer.fingerprint)
    assert reuse.pair(offer.code)["code"] == "not_pairing"          # one-time: the window is used up
    offer = rig.core.bridge.offer_pairing(rig.session)
    for _ in range(5):
        attempt = Phone().connect(offer.port, offer.fingerprint)
        assert attempt.pair("WRNG-CODE")["code"] in ("bad_code", "not_pairing")
    assert not rig.core.devices.pairing_open()                       # brute force closes the window
    late = Phone().connect(offer.port, offer.fingerprint)
    assert late.pair(offer.code)["code"] == "not_pairing"


def test_a_phone_that_cant_sign_with_its_key_is_refused(rig):
    phone, ready, offer = paired(rig)
    phone.close()
    impostor = Phone().connect(offer.port, offer.fingerprint)        # a different key claiming the device id
    reply = impostor.auth(ready["device"])
    assert reply["t"] == "error" and reply["code"] == "bad_signature"
    replay = Phone(phone.key).connect(offer.port, offer.fingerprint)
    replay.send({"t": "auth", "device": ready["device"], "sig": replay.sign(nonce=b"\0" * 32)})  # old challenge
    assert replay.recv()["code"] == "bad_signature"
    assert "auth_failed" in [e["event"] for e in rig.core.devices.events(rig.session)]


def test_removing_a_phone_closes_it_and_it_must_pair_again(rig):
    phone, ready, offer = paired(rig)
    assert wait_for(lambda: ready["device"] in rig.core.bridge.connected_devices())
    assert rig.core.devices.revoke(rig.session, ready["device"])
    assert wait_for(lambda: ready["device"] not in rig.core.bridge.connected_devices())
    back = Phone(phone.key).connect(offer.port, offer.fingerprint)
    assert back.auth(ready["device"])["code"] == "unknown_device"


# -- talking to DUMBOO from the phone --------------------------------------------------------------------------------
def test_chat_uses_dumboo_commands_and_ai_without_touching_the_laptop_conversation(rig):
    phone, _ready, _offer = paired(rig)
    phone.send({"t": "chat", "id": 1, "text": "add a task to buy milk"})
    answer = phone.until("answer")
    assert answer["id"] == 1 and "TASKS" in answer["text"] and answer["kind"] == "task"
    assert [t.title for t in rig.core.tasks.list(rig.session, False)] == ["Buy milk"]
    phone.send({"t": "chat", "id": 2, "text": "tell me something nice"})
    answer = phone.until("answer")
    assert answer["id"] == 2 and "Answer to: tell me something nice" in answer["text"]
    assert rig.core.conversation.context.turn_count == 0              # the laptop's own conversation is untouched


def test_push_to_talk_is_transcribed_on_the_laptop(rig):
    phone, _ready, _offer = paired(rig)
    pcm = (np.sin(np.linspace(0, 400, 16000)) * 8000).astype("<i2").tobytes()   # 1 s of sound
    phone.send({"t": "voice", "id": 7, "rate": 16000, "pcm": base64.b64encode(pcm).decode()})
    assert phone.until("transcript")["text"] == "hi"                  # the test Whisper always hears "hi"
    assert phone.until("answer")["id"] == 7
    phone.send({"t": "voice", "id": 8, "rate": 44100, "pcm": base64.b64encode(pcm).decode()})
    assert phone.until("error")["code"] == "bad_request"


def test_agenda_and_reminders_reach_the_phone(rig):
    phone, _ready, _offer = paired(rig)
    phone.send({"t": "chat", "id": 1, "text": "add a task to submit the report by tomorrow at 5 pm"})
    phone.until("answer")
    phone.send({"t": "agenda.get"})
    agenda = phone.until("agenda")
    assert agenda["tasks"][0]["title"] == "Submit the report" and agenda["tasks"][0]["has_time"]
    rig.core.bus.publish(Events.REMINDER_DUE, {"title": "Call mom", "kind": "event",
                                               "starts_at": "2030-01-01T17:00"})
    reminder = phone.until("reminder")                                # forwarded: the laptop has this user signed in
    assert reminder["title"] == "Call mom" and "Call mom" in reminder["text"]


# -- safety gates -------------------------------------------------------------------------------------------------------
def test_the_phone_cannot_control_the_laptop_yet(rig):
    phone, _ready, _offer = paired(rig)
    desktop = rig.core.desktop
    phone.send({"t": "chat", "id": 1, "text": "open notepad"})
    answer = phone.until("answer")
    assert "switched off" in answer["text"] and answer["kind"] == "automation"
    assert not getattr(desktop, "launched", []) and not getattr(desktop, "opened", [])
    phone.send({"t": "chat", "id": 2, "text": "disable the startup entry OneDrive"})
    assert "switched off" in phone.until("answer")["text"]


def test_capabilities_are_checked_on_the_laptop(rig):
    phone, ready, _offer = paired(rig)
    phone.close()
    rig.core.devices.set_capability(rig.session, ready["device"], "chat", False)
    again = Phone(phone.key).connect(rig.core.bridge.port, rig.core.bridge.identity.fingerprint())
    assert again.auth(ready["device"])["caps"]["chat"] is False
    again.send({"t": "chat", "id": 1, "text": "hello"})
    assert again.until("error")["code"] == "not_allowed"


def test_the_phone_session_ends_when_it_disconnects_and_floods_are_limited(rig):
    phone, ready, _offer = paired(rig)
    for _ in range(25):
        phone.send({"t": "ping"})
    kinds = []
    for _ in range(25):
        kinds.append(phone.recv()["t"])
    assert "error" in kinds                                            # 20 messages per 10 s
    assert wait_for(lambda: ready["device"] in rig.core.bridge.connected_devices())
    [(link, _conn)] = rig.core.bridge._links[ready["device"]]
    phone.close()
    assert wait_for(lambda: ready["device"] not in rig.core.bridge.connected_devices())
    with pytest.raises(Exception):
        rig.core.auth.require(link.session)                           # the phone's session was ended


def test_the_bridge_is_off_until_a_phone_is_paired(paths):
    r = Rig(paths, bridge_overrides={"identity": {"protect": lambda b: b, "unprotect": lambda b: b}})
    try:
        assert not r.core.bridge.running and not r.core.settings.get("bridge", "enabled")
    finally:
        r.close()
