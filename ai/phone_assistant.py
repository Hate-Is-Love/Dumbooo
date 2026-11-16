"""“… on my phone” by voice or text: DUMBOO controls the paired Android phone (DUMBOO Mobile Phase 3–4).

* Calls: "call Mom on my phone", "dial 9876543210", "call Rahul" (when the phone is connected and calls are on)
* Music: "play lofi on my phone", "pause / resume / next song on my phone", "turn up the volume on my phone"
* Find my phone: "find my phone", "ring my phone", "stop ringing"
* Status: "what's my phone battery", "is my phone charging"
* Notifications: "read my phone notifications", "any messages on my phone", "reply to Rahul saying I'm on my way"
* Clipboard: "send my clipboard to my phone", "copy good morning to my phone"
* Apps: "open WhatsApp on my phone"; screen: "show my phone screen" (opens the phone panel on the laptop)
* Files: "send report.pdf to my phone" (from Desktop, Documents or Downloads)

Everything needs the phone connected and the matching feature switched on in the app; DUMBOO says what's missing.
Other messages pass through unchanged.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable

from ai.controller import ConversationHooks, LocalReply
from services.auth_service import AuthError, Session

log = logging.getLogger(__name__)

_PHONE = r"(?:my |the )?(?:phone|mobile|cell|android)"
_ON = rf"(?: (?:on|from|using|with|via) {_PHONE})"
_LEAD = re.compile(r"^\s*(?:(?:hey|hi|ok|okay|yo)\s+)?(?:dumboo|dumbo)?\s*[,:]?\s*(?:please\s+|can you\s+|could you\s+)?",
                   re.I)

RE_CALL = re.compile(rf"^(?:call|ring up|phone|dial|make a call to)\s+(?P<who>.+?){_ON}?$", re.I)
RE_PLAY = re.compile(rf"^play\s+(?P<what>.+?){_ON}$", re.I)
RE_MEDIA = re.compile(rf"^(?P<verb>pause|resume|play|stop|skip|next|previous|go back)(?: the)?(?: (?:music|song|track|"
                      rf"video|audio|it))?(?: song| track)?{_ON}$|^(?P<verb2>next|previous) (?:song|track){_ON}?$", re.I)
RE_VOLUME = re.compile(rf"^(?:turn |set )?(?:the )?volume (?P<dir>up|down)(?: on {_PHONE})?$|^turn (?P<dir2>up|down) "
                       rf"(?:the )?volume{_ON}$|^(?P<mute>mute|unmute) {_PHONE}$", re.I)
RE_FIND = re.compile(rf"^(?:find|ring|locate|where(?:'s| is)) {_PHONE}\??$|^make {_PHONE} ring$", re.I)
RE_STOP_RING = re.compile(rf"^(?:stop (?:the )?ringing|stop ringing {_PHONE}|i found (?:it|{_PHONE}))$", re.I)
RE_BATTERY = re.compile(rf"^(?:what(?:'s| is)|how(?:'s| is)|check) {_PHONE}(?:'s)? battery(?: level)?\??$|"
                        rf"^{_PHONE}(?:'s)? battery\??$|^is {_PHONE} charging\??$|^how much battery (?:is left )?"
                        rf"(?:on|does) {_PHONE}(?: have)?\??$", re.I)
RE_NOTIFICATIONS = re.compile(rf"^(?:read|show|check|any|what are)(?: me)?(?: the| my| any)? (?:phone |mobile )?"
                              rf"(?:notifications|messages|alerts)(?:{_ON}| on {_PHONE})?\??$|^(?:do i have )?any "
                              rf"(?:new )?(?:notifications|messages){_ON}\??$", re.I)
RE_REPLY = re.compile(r"^(?:reply|respond|answer|text back) to (?P<who>.+?) (?:saying|that|with|:)\s*(?P<msg>.+)$", re.I)
RE_CLIP_SEND = re.compile(rf"^(?:send|share|copy) (?:my |the )?clipboard to {_PHONE}$", re.I)
RE_CLIP_TEXT = re.compile(rf"^copy (?P<text>.+?) to {_PHONE}(?:'s)?(?: clipboard)?$", re.I)
RE_OPEN = re.compile(rf"^(?:open|launch|start) (?P<app>.+?) on {_PHONE}$", re.I)
RE_SCREEN = re.compile(rf"^(?:show|share|mirror|open)(?: me)? {_PHONE}(?:'s)? screen(?: on (?:the )?laptop)?$|"
                       rf"^control {_PHONE}$", re.I)
RE_FILE = re.compile(rf"^send (?P<file>.+?) to {_PHONE}$", re.I)


def _clean(text: str) -> str:
    return re.sub(r"[\s.!]+$", "", _LEAD.sub("", text or "").strip())


class PhoneAssistant(ConversationHooks):
    def __init__(self, inner: ConversationHooks, auth, bridge: Callable[[], Any],
                 open_panel: Callable[[str], None] = lambda tab: None) -> None:
        self._inner = inner
        self._auth = auth
        self._bridge = bridge
        self._open_panel = open_panel

    def prompt_options(self, scope: Any) -> dict:
        return self._inner.prompt_options(scope)

    def extra_context(self, scope: Any, text: str, seq: int) -> str:
        return self._inner.extra_context(scope, text, seq)

    def forget_session(self, scope) -> None:
        if hasattr(self._inner, "forget_session"):
            self._inner.forget_session(scope)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def local_reply(self, scope: Any, text: str, source: str) -> LocalReply | None:
        if isinstance(scope, Session) and source != "phone":
            try:
                reply = self._handle(scope, _clean(text))
            except AuthError as exc:
                return LocalReply(exc.user_message, kind="phone")
            except Exception as exc:
                from bridge.phone import PhoneError

                if isinstance(exc, PhoneError):
                    return LocalReply(f"📱 {exc.message}", speech=exc.message, kind="phone")
                log.exception("Phone command failed")
                return LocalReply("Something went wrong talking to your phone.", kind="phone")
            if reply is not None:
                return reply
        return self._inner.local_reply(scope, text, source)

    # -- helpers ---------------------------------------------------------------------------------------------------
    def _phone(self, scope):
        bridge = self._bridge()
        return bridge.phone_for(scope) if bridge is not None else None

    def _do(self, scope, action: str, args: dict | None = None, timeout: float = 12.0) -> dict:
        from bridge.phone import PhoneError

        bridge = self._bridge()
        if bridge is None:
            raise PhoneError("The phone link isn't available.")
        return bridge.phone_request(scope, action, args, timeout)

    @staticmethod
    def _ok(text: str, speech: str | None = None) -> LocalReply:
        return LocalReply(f"📱 {text}", speech=speech or text, kind="phone")

    def _handle(self, scope: Session, t: str) -> LocalReply | None:
        if not t:
            return None
        mentions_phone = bool(re.search(r"\b(?:phone|mobile|cell|android)\b", t, re.I))
        m = RE_FIND.match(t)
        if m:
            self._do(scope, "ring")
            return self._ok("Your phone is ringing at full volume. Say “stop ringing” when you've found it.",
                            "Your phone is ringing now.")
        if RE_STOP_RING.match(t) and self._phone(scope) is not None:
            self._do(scope, "stop_ring")
            return self._ok("Stopped ringing.")
        if RE_BATTERY.match(t):
            status = self._do(scope, "status").get("data", {})
            level, charging = status.get("battery"), status.get("charging")
            if level is None:
                return self._ok("Your phone didn't report its battery.")
            text = f"Your phone is at {level}%{' and charging' if charging else ''}."
            return self._ok(text)
        if RE_NOTIFICATIONS.match(t) and mentions_phone | bool(self._phone(scope)):
            link = self._phone(scope)
            if link is None:
                return None
            if not link.phone.can("notifications"):
                return self._ok("Notification sharing is switched off on your phone (DUMBOO Mobile → Settings).")
            notes = list(link.phone.notifications)[:8]
            if not notes:
                return self._ok("No notifications on your phone right now.")
            lines = [f"- **{n['app']}** · {n['title']}: {n['text']}".rstrip(": ") for n in notes]
            spoken = "; ".join(f"{n['app']}: {n['title']}" for n in notes[:4])
            return LocalReply("📱 **Phone notifications**\n\n" + "\n".join(lines) +
                              "\n\n_Say “reply to … saying …” to answer a message._",
                              speech=f"You have {len(notes)} notifications. {spoken}.", kind="phone")
        m = RE_REPLY.match(t)
        if m and self._phone(scope) is not None:
            link = self._phone(scope)
            note = link.phone.find_notification(m.group("who"), replyable=True)
            if note is None:
                return self._ok(f"I couldn't find a message from “{m.group('who')}” on your phone that can be "
                                "answered.")
            self._do(scope, "reply", {"key": note["key"], "text": m.group("msg")[:1000]})
            return self._ok(f"Replied to {note['title']} ({note['app']}): “{m.group('msg')}”",
                            f"Replied to {note['title']}.")
        if RE_CLIP_SEND.match(t):
            bridge = self._bridge()
            text = bridge._core.desktop.get_clipboard() if bridge is not None else None
            if not text:
                return self._ok("The laptop's clipboard is empty (or isn't text).")
            self._do(scope, "clipboard.set", {"text": text[:20000]})
            return self._ok("Sent the laptop's clipboard to your phone. Long-press any text box there to paste.",
                            "Sent your clipboard to your phone.")
        m = RE_CLIP_TEXT.match(t)
        if m:
            self._do(scope, "clipboard.set", {"text": m.group("text")[:20000]})
            return self._ok(f"Copied “{m.group('text')}” to your phone's clipboard.")
        m = RE_OPEN.match(t)
        if m:
            result = self._do(scope, "open_app", {"name": m.group("app")})
            return self._ok(result.get("message") or f"Opened {m.group('app')} on your phone.")
        if RE_SCREEN.match(t):
            if self._phone(scope) is None:
                return None
            self._open_panel("screen")
            return self._ok("Opening your phone's screen on the laptop. Accept the screen-sharing prompt on your "
                            "phone.", "Accept the screen sharing prompt on your phone.")
        m = RE_PLAY.match(t)
        if m:
            result = self._do(scope, "play", {"query": m.group("what")})
            return self._ok(result.get("message") or f"Playing {m.group('what')} on your phone.")
        m = RE_MEDIA.match(t)
        if m:
            verb = (m.group("verb") or m.group("verb2") or "").lower()
            key = {"pause": "pause", "stop": "pause", "resume": "play", "play": "play", "skip": "next",
                   "next": "next", "previous": "previous", "go back": "previous"}[verb]
            self._do(scope, "media", {"key": key})
            return self._ok({"pause": "Paused.", "play": "Playing.", "next": "Next track.",
                             "previous": "Previous track."}[key])
        m = RE_VOLUME.match(t)
        if m:
            direction = (m.group("dir") or m.group("dir2") or m.group("mute") or "").lower()
            self._do(scope, "volume", {"direction": direction})
            return self._ok({"up": "Volume up.", "down": "Volume down.", "mute": "Muted your phone.",
                             "unmute": "Unmuted your phone."}[direction])
        m = RE_FILE.match(t)
        if m and not re.match(r"^(?:my |the )?clipboard$", m.group("file"), re.I):
            path = self._find_file(m.group("file"))
            if path is None:
                return self._ok(f"I couldn't find “{m.group('file')}” on your Desktop, Documents or Downloads. Use "
                                "the phone panel (ACCOUNT → Phone → Phone panel) to pick a file.")
            bridge = self._bridge()
            bridge.send_file_to_phone(scope, path)
            return self._ok(f"Sent **{path.name}** to your phone (Downloads folder).", f"Sent {path.name} to your phone.")
        m = RE_CALL.match(t)
        if m:
            link = self._phone(scope)
            if link is None or not (mentions_phone or link.phone.can("calls")):
                return None    # "call mom" without a phone: not ours
            who = re.sub(r"^(?:to\s+)?", "", m.group("who"), flags=re.I).strip()
            number = re.sub(r"[\s-]", "", who)
            action = "dial" if re.fullmatch(r"\+?\d{3,15}", number) else "call"
            result = self._do(scope, action, {"who": number if action == "dial" else who})
            return self._ok(result.get("message") or f"Calling {who} on your phone…")
        return None

    @staticmethod
    def _find_file(name: str) -> Path | None:
        name = name.strip().strip("\"'")
        candidate = Path(name).expanduser()
        if candidate.is_file():
            return candidate
        home = Path.home()
        for folder in (home / "Desktop", home / "Documents", home / "Downloads", home / "OneDrive" / "Desktop",
                       home / "OneDrive" / "Documents"):
            path = folder / name
            if path.is_file():
                return path
        return None
