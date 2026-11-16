"""“… on my phone” by voice or text: DUMBOO controls the paired Android phone (DUMBOO Mobile Phase 3–4).

* Calls: "call Mom on my phone", "dial 9876543210", "call Rahul" (when the phone is connected and calls are on)
* Music: "play lofi on my phone", "pause / resume / next song on my phone", "turn up the volume on my phone"
* Find my phone: "find my phone", "ring my phone", "stop ringing"
* Status: "what's my phone battery", "is my phone charging"
* Notifications: "read my phone notifications", "any messages on my phone", "reply to Rahul saying I'm on my way"
* Clipboard: "send my clipboard to my phone", "copy good morning to my phone"
* Apps: "open WhatsApp on my phone"; screen: "show my phone screen" (opens the phone panel on the laptop)
* Files: "send report.pdf to my phone" (from Desktop, Documents or Downloads)

* More: "turn on the torch", "set an alarm for 6:30 am", "set a timer for 10 minutes", "navigate to the airport",
  "search YouTube for lofi", "text Mom saying I'm on my way" (each "… on my phone" from the laptop)

From the laptop, everything needs the phone connected and the matching feature switched on in the app; DUMBOO says
what's missing. Spoken or typed INTO the phone (push-to-talk in DUMBOO Mobile), the same commands work without
"on my phone" and act on that phone: "call mom", "play Arijit Singh", "open WhatsApp", "set an alarm for 7". Anything
that isn't a phone command (or names the laptop) passes through unchanged, so "open notepad" still reaches the laptop
when the phone has no app with that name.
"""

from __future__ import annotations

import logging
import re
import time
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
RE_ON_PHONE = re.compile(rf"{_ON}$", re.I)
RE_LAPTOP = re.compile(r"\b(?:laptop|computer|pc|desktop|windows)\b", re.I)
RE_TORCH = re.compile(r"^(?:turn |switch )?(?P<on>on|off) (?:the )?(?:torch|flash ?light)$|"
                      r"^(?:turn |switch )?(?:the )?(?:torch|flash ?light) (?P<on2>on|off)$", re.I)
RE_ALARM = re.compile(r"^(?:set (?:an |the |my )?alarm|wake me(?: up)?)(?: for| at)? (?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?"
                      r"\s*(?P<ap>am|pm|a\.m\.?|p\.m\.?)?(?: tomorrow)?$", re.I)
RE_TIMER = re.compile(r"^(?:set |start )?(?:a |the )?timer (?:for )?(?P<n>\d+|a|an|one|two|three|five|ten|fifteen|"
                      r"twenty|thirty) ?(?P<u>seconds?|secs?|minutes?|mins?|hours?|hrs?)$|^(?:set |start )?(?:a )?"
                      r"(?P<n2>\d+|a|an|one|two|three|five|ten|fifteen|twenty|thirty)[ -]"
                      r"(?P<u2>second|sec|minute|min|hour|hr)s? timer$", re.I)
RE_NAV = re.compile(r"^(?:navigate|get directions|directions|take me|drive me) to (?P<place>.+)$", re.I)
RE_YT = re.compile(r"^search (?:on )?youtube for (?P<q>.+)$|^search (?:for )?(?P<q2>.+?) on youtube$", re.I)
RE_SMS = re.compile(r"^(?:text|sms|send (?:a )?(?:text|message|sms) to|message) (?P<who>.+?) (?:saying|that|:)\s*"
                    r"(?P<msg>.+)$", re.I)
RE_OPEN_ANY = re.compile(r"^(?:open|launch|start) (?:the )?(?P<app>[\w .'&+-]{2,40}?)(?: app)?$", re.I)
ORDINALS = {"first": 0, "one": 0, "1": 0, "1st": 0, "second": 1, "two": 1, "2": 1, "2nd": 1, "third": 2, "three": 2,
            "3": 2, "3rd": 2, "fourth": 3, "four": 3, "4": 3, "4th": 3, "fifth": 4, "five": 4, "5": 4, "5th": 4,
            "sixth": 5, "six": 5, "6": 5}
CANCEL = re.compile(r"^(?:cancel|never ?mind|none|none of them|no|nothing|stop|forget it)$", re.I)
CHOICE_TTL_S = 120
WORD_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "five": 5, "ten": 10, "fifteen": 15, "twenty": 20,
                "thirty": 30}


def _key(scope) -> str:
    return getattr(scope, "token", None) or str(id(scope))


def _clean(text: str) -> str:
    return re.sub(r"[\s.!]+$", "", _LEAD.sub("", text or "").strip())


class PhoneAssistant(ConversationHooks):
    def __init__(self, inner: ConversationHooks, auth, bridge: Callable[[], Any],
                 open_panel: Callable[[str], None] = lambda tab: None,
                 lookup: Callable[[str], str | None] = lambda query: None) -> None:
        self._inner = inner
        self._auth = auth
        self._bridge = bridge
        self._open_panel = open_panel
        self._lookup = lookup                    # song -> YouTube video id, so the phone plays it directly
        self._last: dict[str, tuple] = {}        # session -> the last phone command (for follow-up choices)
        self._pending: dict[str, dict] = {}      # session -> a question with choices DUMBOO is waiting on

    def prompt_options(self, scope: Any) -> dict:
        return self._inner.prompt_options(scope)

    def extra_context(self, scope: Any, text: str, seq: int) -> str:
        return self._inner.extra_context(scope, text, seq)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def local_reply(self, scope: Any, text: str, source: str) -> LocalReply | None:
        if isinstance(scope, Session):
            reply = self._answer_choice(scope, text)
            if reply is not None:
                return reply
        if isinstance(scope, Session) and source == "phone":
            reply = self._from_the_phone(scope, _clean(text))
            if reply is not None:
                return reply
        if isinstance(scope, Session) and source != "phone":
            try:
                reply = self._handle(scope, _clean(text))
            except AuthError as exc:
                return LocalReply(exc.user_message, kind="phone")
            except Exception as exc:
                from bridge.phone import PhoneError

                if isinstance(exc, PhoneError):
                    return self._ask_choice(scope, exc) or LocalReply(f"📱 {exc.message}", speech=exc.message,
                                                                       kind="phone")
                log.exception("Phone command failed")
                return LocalReply("Something went wrong talking to your phone.", kind="phone")
            if reply is not None:
                return reply
        return self._inner.local_reply(scope, text, source)

    # -- spoken or typed on the phone, for the phone ---------------------------------------------------------------
    def _from_the_phone(self, scope: Session, t: str) -> LocalReply | None:
        from bridge.phone import PhoneError

        bridge = self._bridge()
        link = bridge.link_for_session(scope) if bridge is not None and hasattr(bridge, "link_for_session") else None
        if link is None or not link.phone.features or not t or RE_LAPTOP.search(t):
            return None          # an older app, or meant for the laptop: the normal chain handles it
        t = RE_ON_PHONE.sub("", t).strip()
        command = self._phone_command(t, own=True)
        if command is None:
            return None
        action, args, done = command
        try:
            result = self._send_cmd(scope, action, args, link)
        except PhoneError as exc:
            if action == "open_app" and "No app called" in exc.message:
                return None      # "open notepad": not a phone app, so it's for the laptop
            return self._ask_choice(scope, exc) or LocalReply(f"📱 {exc.message}", speech=exc.message, kind="phone")
        message = result.get("message") or done
        return LocalReply(f"📱 {message}", speech=message, kind="phone")

    @staticmethod
    def _phone_command(t: str, own: bool) -> tuple[str, dict, str] | None:
        """Phone actions in plain words (no "on my phone"). ``own``: said on the phone itself."""
        m = RE_TORCH.match(t)
        if m:
            on = (m.group("on") or m.group("on2")).lower() == "on"
            return "torch", {"on": on}, f"Torch {'on' if on else 'off'}."
        m = RE_ALARM.match(t)
        if m:
            hour, minute = int(m.group("h")), int(m.group("m") or 0)
            ap = (m.group("ap") or "").lower()[:1]
            if ap == "p" and hour < 12:
                hour += 12
            if ap == "a" and hour == 12:
                hour = 0
            if hour > 23 or minute > 59:
                return None
            return "alarm", {"hour": hour, "minute": minute}, f"Alarm set for {hour:02d}:{minute:02d}."
        m = RE_TIMER.match(t)
        if m:
            n, unit = (m.group("n") or m.group("n2")).lower(), (m.group("u") or m.group("u2")).lower()
            count = int(n) if n.isdigit() else WORD_NUMBERS[n]
            seconds = count * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)
            if not 0 < seconds <= 24 * 3600:
                return None
            return "timer", {"seconds": seconds}, f"Timer set for {count} {unit}."
        m = RE_NAV.match(t)
        if m:
            return "navigate", {"place": m.group("place")}, f"Directions to {m.group('place')}."
        m = RE_YT.match(t)
        if m:
            q = m.group("q") or m.group("q2")
            return "search", {"query": q, "site": "youtube"}, f"Searching YouTube for {q}."
        m = RE_SMS.match(t)
        if m:
            return "sms", {"who": m.group("who"), "text": m.group("msg")[:1000]}, \
                f"Opened a text to {m.group('who')}: tap Send on the phone."
        if not own:
            return None
        m = RE_CALL.match(t)
        if m:
            who = re.sub(r"^(?:to\s+)?", "", m.group("who"), flags=re.I).strip()
            number = re.sub(r"[\s-]", "", who)
            if re.fullmatch(r"\+?\d{3,15}", number):
                return "dial", {"who": number}, f"Calling {number}…"
            return "call", {"who": who}, f"Calling {who}…"
        m = RE_MEDIA.match(t + " on my phone") or RE_MEDIA.match(t)
        if m:
            verb = (m.group("verb") or m.group("verb2") or "").lower()
            key = {"pause": "pause", "stop": "pause", "resume": "play", "play": "play", "skip": "next",
                   "next": "next", "previous": "previous", "go back": "previous"}[verb]
            return "media", {"key": key}, {"pause": "Paused.", "play": "Playing.", "next": "Next track.",
                                           "previous": "Previous track."}[key]
        m = RE_VOLUME.match(t) or RE_VOLUME.match(t + " on my phone")
        if m:
            direction = (m.group("dir") or m.group("dir2") or m.group("mute") or "").lower()
            return "volume", {"direction": direction}, "Done."
        if re.match(r"^(?:mute|unmute)(?: the)?(?: phone| sound)?$", t, re.I):
            return "volume", {"direction": t.split()[0].lower()}, "Done."
        m = re.match(r"^play (?P<what>.+)$", t, re.I)
        if m:
            return "play", {"query": m.group("what")}, f"Playing {m.group('what')}."
        m = RE_OPEN_ANY.match(t)
        if m:
            return "open_app", {"name": m.group("app")}, f"Opened {m.group('app')}."
        return None

    # -- helpers ---------------------------------------------------------------------------------------------------
    def _phone(self, scope):
        bridge = self._bridge()
        return bridge.phone_for(scope) if bridge is not None else None

    def _do(self, scope, action: str, args: dict | None = None, timeout: float = 12.0) -> dict:
        return self._send_cmd(scope, action, args, None, timeout)

    def _send_cmd(self, scope, action: str, args: dict | None, link=None, timeout: float = 12.0) -> dict:
        """Send one command to the phone (``link``: the phone the owner is talking on, for itself)."""
        from bridge.phone import PhoneError

        bridge = self._bridge()
        if bridge is None:
            raise PhoneError("The phone link isn't available.")
        args = dict(args or {})
        if action == "play" and args.get("query") and "video" not in args:
            try:
                video = self._lookup(args["query"])
            except Exception:
                video = None
            if video:
                args["video"] = video          # the phone opens it and it plays at once
        self._last[_key(scope)] = (action, args, link)
        if link is not None:
            return bridge.own_phone_request(link, action, args, timeout)
        return bridge.phone_request(scope, action, args, timeout)

    # -- choices: "which Rahul?" → "the second one" -------------------------------------------------------------------
    def _ask_choice(self, scope, exc) -> LocalReply | None:
        choices = [c for c in (exc.data or {}).get("choices", []) if isinstance(c, dict) and c.get("id")][:6]
        last = self._last.get(_key(scope))
        if len(choices) < 2 or last is None:
            return None
        action, args, link = last
        self._pending[_key(scope)] = {"action": action, "args": args, "link": link, "choices": choices,
                                      "at": time.monotonic()}
        numbers = ["One", "Two", "Three", "Four", "Five", "Six"]
        lines = "\n".join(f"{i + 1}. {c.get('label') or c['id']}" for i, c in enumerate(choices))
        spoken = " ".join(f"{numbers[i]}: {c.get('label') or c['id']}." for i, c in enumerate(choices))
        question = exc.message or "Which one?"
        return LocalReply(f"📱 {question}\n\n{lines}\n\n_Say the number or the name, or “cancel”._",
                          speech=f"{question} {spoken} Which one?", kind="phone", listen_after=True)

    def _answer_choice(self, scope, text: str) -> LocalReply | None:
        from bridge.phone import PhoneError

        pending = self._pending.get(_key(scope))
        if pending is None:
            return None
        if time.monotonic() - pending["at"] > CHOICE_TTL_S:
            self._pending.pop(_key(scope), None)
            return None
        t = re.sub(r"^(?:the |number |option |no\.? )+", "", _clean(text).lower()).strip()
        t = re.sub(r"\s+(?:one|option)$", "", t) if t not in ORDINALS else t
        if CANCEL.match(t):
            self._pending.pop(_key(scope), None)
            return LocalReply("📱 Okay, cancelled.", speech="Okay, cancelled.", kind="phone")
        choices = pending["choices"]
        index = ORDINALS.get(t)
        if t in ("last", "last one"):
            index = len(choices) - 1
        if index is None:
            words = [w for w in re.findall(r"[a-z0-9]+", t) if w not in ("the", "call", "open", "play", "to", "on")]
            matches = [i for i, c in enumerate(choices)
                       if words and all(w in str(c.get("label") or c["id"]).lower() for w in words)]
            index = matches[0] if len(matches) == 1 else None
        if index is None or index >= len(choices):
            self._pending.pop(_key(scope), None)
            return None                      # not an answer to the question: handle the message normally
        self._pending.pop(_key(scope), None)
        choice = choices[index]
        try:
            result = self._send_cmd(scope, pending["action"], {**pending["args"], "pick": choice["id"]},
                                    pending["link"])
        except PhoneError as exc:
            return LocalReply(f"📱 {exc.message}", speech=exc.message, kind="phone")
        message = result.get("message") or f"Done: {choice.get('label') or choice['id']}."
        return LocalReply(f"📱 {message}", speech=message, kind="phone")

    def forget_session(self, scope) -> None:
        self._pending.pop(_key(scope), None)
        self._last.pop(_key(scope), None)
        if hasattr(self._inner, "forget_session"):
            self._inner.forget_session(scope)

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
        on_phone = re.sub(rf"(?: on {_PHONE})", "", t, count=1, flags=re.I) if re.search(
            rf" on {_PHONE}\b", t, re.I) else None
        command = self._phone_command(on_phone.strip(), own=False) if on_phone else None
        if command is not None:
            action, args, done = command
            result = self._do(scope, action, args)
            return self._ok(result.get("message") or done)
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
