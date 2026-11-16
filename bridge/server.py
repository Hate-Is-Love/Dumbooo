"""The phone link server: TLS WebSocket, pairing and challenge-response authentication, one handler per phone.

``PhoneBridge`` is what the rest of DUMBOO uses: start/stop (lifecycle), the pairing QR payload, the list of paired
phones, removal, and forwarding the laptop's reminders to that user's phones.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import secrets
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from app.event_bus import Events
from bridge import protocol as P
from bridge.devices import DeviceRegistry
from bridge.identity import LaptopIdentity
from bridge.link import PhoneLink

log = logging.getLogger(__name__)


def local_addresses() -> list[str]:
    """IPv4 addresses the phone can try: Wi-Fi/Ethernet first, then Tailscale (100.64.0.0/10)."""
    import psutil

    lan, tailnet = [], []
    for _name, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family != socket.AF_INET:
                continue
            ip = ipaddress.ip_address(a.address)
            if ip.is_loopback or ip.is_link_local:
                continue
            (tailnet if ip in ipaddress.ip_network("100.64.0.0/10") else lan).append(a.address)
    return lan + tailnet


@dataclass
class PairingOffer:
    uri: str
    code: str
    expires_in_s: int
    addresses: list[str]
    port: int
    fingerprint: str


class PhoneBridge:
    name = "bridge"

    def __init__(self, core, settings: Callable[[], dict], identity: LaptopIdentity, registry: DeviceRegistry,
                 addresses: Callable[[], list[str]] = local_addresses, host: str = "0.0.0.0") -> None:
        self._core = core
        self._settings = settings
        self.identity = identity
        self.registry = registry
        self._addresses = addresses
        self._host = host
        self._server = None
        self._thread: threading.Thread | None = None
        self._links: dict[int, list] = {}          # device id -> [(link, connection)]
        self._lock = threading.Lock()
        self.last_error = ""
        self.port = 0
        self.remote_factory = None           # tests inject a fake RemoteControl factory
        registry.on_revoked = self._close_device
        registry.on_capabilities = self._capabilities_changed
        core.bus.subscribe(Events.REMINDER_DUE, self._forward_reminder)

    # -- lifecycle ------------------------------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running or not self._settings().get("enabled"):
            return
        from websockets.sync.server import serve

        try:
            ctx = self.identity.server_context()
            self._server = serve(self._handle, self._host, int(self._settings().get("port", 8765)), ssl=ctx,
                                 max_size=P.MAX_FRAME_BYTES, compression=None, ping_interval=20, ping_timeout=20,
                                 open_timeout=P.AUTH_TIMEOUT_S, server_header=None)
        except OSError as exc:
            self.last_error = f"could not listen on port {self._settings().get('port', 8765)}: {exc.strerror or exc}"
            log.warning("Phone link not started: %s", self.last_error)
            return
        self.port = self._server.socket.getsockname()[1]
        self.last_error = ""
        self._thread = threading.Thread(target=self._server.serve_forever, name="PhoneBridge", daemon=True)
        self._thread.start()
        log.info("Phone link listening on port %s", self.port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._server = self._thread = None
        with self._lock:
            links = [item for items in self._links.values() for item in items]
            self._links.clear()
        for link, connection in links:
            link.close()
            try:
                connection.close(1001, "DUMBOO is closing")
            except Exception:
                pass

    def set_enabled(self, enabled: bool) -> None:
        self._core.settings.update({"bridge": {"enabled": bool(enabled)}})
        if enabled:
            self.start()
        else:
            self.stop()

    def status(self) -> dict:
        with self._lock:
            connected = {d: len(v) for d, v in self._links.items() if v}
        return {"enabled": bool(self._settings().get("enabled")), "running": self.running, "port": self.port,
                "addresses": self._addresses(), "connected": connected, "error": self.last_error}

    # -- pairing --------------------------------------------------------------------------------------------------
    def offer_pairing(self, session) -> PairingOffer:
        if not self.running:
            self.set_enabled(True)
        if not self.running:
            raise P.ProtocolError("not_running", self.last_error or "The phone link couldn't start.")
        code = self.registry.start_pairing(session)
        addresses = self._addresses()
        fingerprint = self.identity.fingerprint()
        data = {"v": P.VERSION, "h": addresses, "p": self.port, "fp": fingerprint, "c": code.code,
                "n": socket.gethostname()[:40]}
        uri = "dumboo://pair?d=" + base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()) \
            .decode().rstrip("=")
        return PairingOffer(uri, code.code, code.expires_in_s, addresses, self.port, fingerprint)

    # -- one phone connection -------------------------------------------------------------------------------------------
    def _handle(self, connection) -> None:
        try:   # small messages (touchpad moves) go out at once instead of waiting to be bundled
            connection.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except (OSError, AttributeError):
            pass
        send_lock = threading.Lock()

        def send(msg: dict) -> None:
            with send_lock:
                try:
                    connection.send(P.dump(msg))
                except Exception:
                    pass

        remote_ip = connection.remote_address[0] if connection.remote_address else ""
        if not self._address_allowed(remote_ip):
            log.warning("Phone link: refused a connection from a public address")
            connection.close(1008, "address")
            return
        nonce = secrets.token_bytes(32)
        fingerprint = self.identity.fingerprint()
        send({"t": "hello", "v": P.VERSION, "nonce": P.b64(nonce)})
        try:
            device = self._authenticate(connection, nonce, fingerprint)
        except P.ProtocolError as exc:
            send({"t": "error", "code": exc.code, "message": exc.message})
            connection.close(1008, exc.code)
            return
        except Exception:
            connection.close(1008, "handshake")
            return
        try:
            session = self._core.auth.login_verified(device.user_id, "phone")
        except Exception as exc:
            send({"t": "error", "code": "account", "message": getattr(exc, "user_message", "Account not available")})
            connection.close(1008, "account")
            return
        remote = self.remote_factory() if self.remote_factory else None
        link = PhoneLink(self._core, device, session, send, remote=remote)
        with self._lock:
            self._links.setdefault(device.id, []).append((link, connection))
        self.registry.audit("connected", device.id, device.user_id, connection.remote_address[0]
                            if connection.remote_address else "")
        send({"t": "ready", "device": device.id, "user": session.display_name, "caps": device.capabilities,
              "addresses": self._addresses(), "tailscale": any(a.startswith("100.") for a in self._addresses())})
        recent: deque[float] = deque()       # chat, voice, agenda: 20 per 10 s
        fast: deque[float] = deque()         # touchpad, keys, screen acks: 400 per 10 s
        try:
            for frame in connection:
                try:
                    msg = P.parse(frame)
                except P.ProtocolError as exc:
                    send({"t": "error", "code": exc.code, "message": exc.message})
                    continue
                bucket, limit = (fast, P.INPUT_RATE_MAX) if msg["t"] in P.INPUT_TYPES else (recent, P.RATE_MAX_MESSAGES)
                now = time.monotonic()
                while bucket and now - bucket[0] > P.RATE_WINDOW_S:
                    bucket.popleft()
                bucket.append(now)
                if len(bucket) > limit:
                    send({"t": "error", "code": "rate_limit", "message": "Too many messages; slow down a little."})
                    continue
                try:
                    link.handle(msg)
                except P.ProtocolError as exc:
                    send({"t": "error", "code": exc.code, "message": exc.message})
                except Exception:
                    log.exception("Phone message failed")
                    send({"t": "error", "code": "internal", "message": "Something went wrong on the laptop."})
        except Exception:
            log.debug("Phone connection ended", exc_info=True)
        finally:
            link.close()   # end the phone's session first, then stop reporting it as connected
            with self._lock:
                items = self._links.get(device.id, [])
                self._links[device.id] = [i for i in items if i[0] is not link]
            self.registry.audit("disconnected", device.id, device.user_id)

    def _address_allowed(self, ip: str) -> bool:
        """Only home/office networks, Tailscale and this PC, unless public addresses are explicitly allowed."""
        if self._settings().get("allow_public"):
            return True
        try:
            addr = ipaddress.ip_address(ip.split("%")[0])
        except ValueError:
            return False
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        return (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr in ipaddress.ip_network("100.64.0.0/10")              # Tailscale / CGNAT
                or addr in ipaddress.ip_network("fd7a:115c:a1e0::/48"))     # Tailscale IPv6

    def _authenticate(self, connection, nonce: bytes, fingerprint: str):
        msg = P.parse(connection.recv(timeout=P.AUTH_TIMEOUT_S))
        if msg["t"] == "pair":
            device = self.registry.pair(
                P.text_field(msg, "code", 20), str(msg.get("name") or "Phone"),
                P.b64decode(msg.get("pub"), "pub", 512), P.b64decode(msg.get("sig"), "sig", 256), nonce, fingerprint)
            return device
        if msg["t"] == "auth":
            return self.registry.authenticate(msg.get("device"), P.b64decode(msg.get("sig"), "sig", 256), nonce,
                                              fingerprint)
        raise P.ProtocolError("bad_request", "expected pair or auth")

    # -- reminders and removal ------------------------------------------------------------------------------------------
    def _forward_reminder(self, _name: str, payload: dict) -> None:
        """The laptop's reminder service fired for the signed-in user: tell that user's phones too."""
        with self._lock:
            links = [link for items in self._links.values() for link, _c in items]
        for link in links:
            if link.laptop_has_this_user():
                link.push_reminder(payload)

    def _close_device(self, device_id: int) -> None:
        with self._lock:
            items = self._links.pop(device_id, [])
        for link, connection in items:
            link.close()
            try:
                connection.close(1008, "removed")
            except Exception:
                pass

    def _capabilities_changed(self, device_id: int) -> None:
        device = self.registry.get(device_id)
        with self._lock:
            links = [link for link, _c in self._links.get(device_id, [])]
        for link in links:
            if device is not None:
                link.update_device(device)

    def allows_control(self, scope) -> bool:
        """Is ``scope`` the session of a connected phone that may control the laptop?"""
        token = getattr(scope, "token_hash", None)
        with self._lock:
            links = [link for items in self._links.values() for link, _c in items]
        return any(link.session.token_hash == token and link.device.can("laptop_control") for link in links)

    # -- laptop -> phone (Phases 3-4) ------------------------------------------------------------------------------
    def phone_for(self, session):
        """The most recently connected phone of the signed-in user, or None."""
        user = self._core.auth.require(session)
        with self._lock:
            links = [link for items in self._links.values() for link, _c in items if link.device.user_id == user.id]
        return links[-1] if links else None

    def _phone(self, session):
        from bridge.phone import PhoneError

        link = self.phone_for(session)
        if link is None:
            raise PhoneError("Your phone isn't connected. Open DUMBOO Mobile on it (same Wi-Fi or Tailscale).")
        return link

    def phone_request(self, session, action: str, args: dict | None = None, timeout: float = 12.0) -> dict:
        link = self._phone(session)
        result = link.phone.request(action, args, timeout)
        self.registry.audit("phone_command", link.device.id, link.device.user_id, action)
        return result

    def link_for_session(self, session):
        """The connected phone whose own session this is (a message spoken or typed on that phone), or None."""
        token = getattr(session, "token", None)
        with self._lock:
            for items in self._links.values():
                for link, _c in items:
                    if token and getattr(link.session, "token", None) == token:
                        return link
        return None

    def own_phone_request(self, link, action: str, args: dict | None = None, timeout: float = 12.0) -> dict:
        """A command the owner gave on the phone, for that phone."""
        result = link.phone.request(action, args, timeout, own=True)
        self.registry.audit("phone_voice_command", link.device.id, link.device.user_id, action)
        return result

    def phone_send(self, session, action: str, args: dict | None = None) -> None:
        self._phone(session).phone.send_nowait(action, args)

    def send_file_to_phone(self, session, path) -> dict:
        link = self._phone(session)
        result = link.phone.send_file(path)
        self.registry.audit("file_to_phone", link.device.id, link.device.user_id, str(path)[-100:])
        return result

    def connected_devices(self) -> set[int]:
        with self._lock:
            return {d for d, v in self._links.items() if v}
