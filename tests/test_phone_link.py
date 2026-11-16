"""DUMBOO Mobile Phases 3-4: the laptop controlling the phone (commands, "my phone" voice commands, notifications,
files and clipboard both ways, the phone's screen) over real TLS WebSocket connections, with a fake phone."""

import base64
import queue
import threading
import time

import pytest

from app.event_bus import Events
from tests.fakes import wait_for
from tests.test_bridge import paired, rig  # noqa: F401  (fixture)

ALL_FEATURES = {"notifications": True, "clipboard": True, "files": True, "calls": True, "music": True,
                "find_phone": True, "open_apps": True, "screen": True}


class ActivePhone:
    """A paired phone that answers the laptop's phone.cmd on a background thread, like the app's service."""

    def __init__(self, phone, answers=None):
        self.phone = phone
        self.cmds: list[dict] = []
        self.inbox: "queue.Queue[dict]" = queue.Queue()
        self.answers = answers or {}
        self.files: dict[str, bytearray] = {}
        self.names: dict[str, str] = {}
        self._run = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while self._run:
            try:
                msg = self.phone.recv(timeout=0.2)
            except TimeoutError:
                continue
            except Exception:
                return
            if msg["t"] == "phone.cmd":
                self.cmds.append(msg)
                if msg["id"]:
                    ok, text, data = (self.answers(msg) if callable(self.answers) else
                                      self.answers.get(msg["action"], (True, f"did {msg['action']}", {})))
                    self.phone.send({"t": "phone.result", "id": msg["id"], "ok": ok, "message": text, "data": data})
            elif msg["t"] == "file.begin":
                self.files[msg["id"]] = bytearray()
                self.names[msg["id"]] = msg["name"]
            elif msg["t"] == "file.chunk":
                self.files[msg["id"]] += base64.b64decode(msg["data"])
            elif msg["t"] == "file.end":
                self.phone.send({"t": "phone.result", "id": msg["reply"], "ok": True, "message": "Saved."})
            else:
                self.inbox.put(msg)

    def until(self, kind, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            msg = self.inbox.get(timeout=max(0.05, end - time.monotonic()))
            if msg["t"] == kind:
                return msg
        raise AssertionError(f"no {kind}")

    def stop(self):
        self._run = False
        self._thread.join(2)


def active_phone(rig, features=ALL_FEATURES, answers=None, tmp=None):  # noqa: F811
    if tmp is not None:
        rig.core.phone_downloads = lambda: tmp
    phone, ready, _offer = paired(rig)
    phone.send({"t": "phone.features", "features": features})
    phone.send({"t": "phone.status", "status": {"battery": 81, "charging": True, "model": "Pixel"}})

    def ready_now():
        link = rig.core.bridge.phone_for(rig.session)
        return link is not None and link.phone.status.get("battery") == 81

    assert wait_for(ready_now, 5)
    return ActivePhone(phone, answers), ready


def test_laptop_commands_reach_the_phone_and_switched_off_features_are_refused(rig):  # noqa: F811
    from bridge.phone import PhoneError

    active, _ready = active_phone(rig, features={**ALL_FEATURES, "calls": False})
    try:
        result = rig.core.bridge.phone_request(rig.session, "ring")
        assert result["ok"] and active.cmds[-1]["action"] == "ring"
        with pytest.raises(PhoneError, match="switched off"):
            rig.core.bridge.phone_request(rig.session, "call", {"who": "Mom"})
        assert all(c["action"] != "call" for c in active.cmds)
        with pytest.raises(PhoneError, match="Unknown"):
            rig.core.bridge.phone_request(rig.session, "factory_reset")
        events = rig.rec.of(Events.PHONE_STATUS)
        assert events and events[-1]["status"]["battery"] == 81
    finally:
        active.stop()


def test_my_phone_voice_commands(rig):  # noqa: F811
    active, _ready = active_phone(rig, answers={"status": (True, "", {"battery": 42, "charging": False}),
                                                "call": (True, "Calling Mom…", {}),
                                                "play": (True, "Playing lofi beats on YouTube Music", {})})
    try:
        assert "ringing" in rig.reply("find my phone")
        assert active.cmds[-1]["action"] == "ring"
        assert "42%" in rig.reply("what's my phone's battery")
        assert "Calling Mom" in rig.reply("call mom on my phone")
        assert active.cmds[-1]["args"] == {"who": "mom"}
        rig.reply("call +91 98765 43210")
        assert active.cmds[-1]["action"] == "dial" and active.cmds[-1]["args"]["who"] == "+919876543210"
        assert "lofi" in rig.reply("play lofi beats on my phone")
        rig.reply("pause the music on my phone")
        assert active.cmds[-1]["args"] == {"key": "pause"}
        rig.reply("open whatsapp on my phone")
        assert active.cmds[-1]["action"] == "open_app" and active.cmds[-1]["args"] == {"name": "whatsapp"}
        rig.reply("copy hello there to my phone")
        assert active.cmds[-1]["action"] == "clipboard.set" and active.cmds[-1]["args"]["text"] == "hello there"
        ready = rig.say("next song on my phone")
        assert ready.get("local") and active.cmds[-1]["args"] == {"key": "next"}   # handled here, not by the AI
    finally:
        active.stop()


def test_notifications_are_listed_and_answered(rig):  # noqa: F811
    active, _ready = active_phone(rig)
    try:
        active.phone.send({"t": "phone.notification", "key": "k1", "app": "WhatsApp", "title": "Priya",
                           "text": "Lunch at 1?", "can_reply": True})
        active.phone.send({"t": "phone.notification", "key": "k2", "app": "Gmail", "title": "Bank",
                           "text": "Statement ready", "can_reply": False})
        assert wait_for(lambda: len(rig.rec.of(Events.PHONE_NOTIFICATION)) == 2, 5)
        text = rig.reply("read my phone notifications")
        assert "Priya" in text and "Bank" in text
        assert "Replied to Priya" in rig.reply("reply to priya saying yes see you then")
        assert active.cmds[-1]["action"] == "reply"
        assert active.cmds[-1]["args"] == {"key": "k1", "text": "yes see you then"}
        assert "couldn't find" in rig.reply("reply to bank saying thanks")    # Gmail offered no reply
        active.phone.send({"t": "phone.notification", "key": "k1", "removed": True})
        link = rig.core.bridge.phone_for(rig.session)
        assert wait_for(lambda: [n["key"] for n in link.phone.notifications] == ["k2"], 5)
    finally:
        active.stop()


def test_files_and_clipboard_both_ways(rig, tmp_path):  # noqa: F811
    active, _ready = active_phone(rig, tmp=tmp_path / "in")
    try:
        data = bytes(range(256)) * 3000                                   # 768 KB: two chunks
        active.phone.send({"t": "file.begin", "id": "f1", "name": "../../evil/photo.jpg", "size": len(data)})
        for i in range(0, len(data), 512 * 1024):
            active.phone.send({"t": "file.chunk", "id": "f1",
                               "data": base64.b64encode(data[i:i + 512 * 1024]).decode()})
        active.phone.send({"t": "file.end", "id": "f1"})
        saved = active.until("file.saved")
        assert saved["name"] == "photo.jpg"
        assert (tmp_path / "in" / "photo.jpg").read_bytes() == data       # the path in the name was ignored
        assert not list((tmp_path / "in").glob("*.part"))
        assert rig.rec.of(Events.PHONE_FILE)[-1]["file"] == "photo.jpg"

        active.phone.send({"t": "clip.push", "text": "copied on the phone"})
        assert wait_for(lambda: rig.core.desktop.get_clipboard() == "copied on the phone", 5)

        out = tmp_path / "notes.txt"
        out.write_bytes(b"x" * 600_000)
        result = rig.core.bridge.send_file_to_phone(rig.session, out)
        assert result["ok"]
        (file_id,) = active.files
        assert bytes(active.files[file_id]) == out.read_bytes() and active.names[file_id] == "notes.txt"

        rig.core.desktop.set_clipboard("from the laptop")
        assert "Sent the laptop's clipboard" in rig.reply("send my clipboard to my phone")
        assert active.cmds[-1]["args"] == {"text": "from the laptop"}
    finally:
        active.stop()


def test_a_file_bigger_than_announced_is_dropped(rig, tmp_path):  # noqa: F811
    active, _ready = active_phone(rig, tmp=tmp_path / "in")
    try:
        active.phone.send({"t": "file.begin", "id": "f2", "name": "a.bin", "size": 10})
        active.phone.send({"t": "file.chunk", "id": "f2", "data": base64.b64encode(b"y" * 20).decode()})
        error = active.until("error")
        assert error["code"] == "bad_request"
        assert not list((tmp_path / "in").iterdir())
    finally:
        active.stop()


def test_phone_screen_frames_reach_the_laptop_only_when_shared(rig):  # noqa: F811
    active, _ready = active_phone(rig, features={**ALL_FEATURES, "screen": False})
    try:
        frame = base64.b64encode(b"\xff\xd8x").decode()
        active.phone.send({"t": "phone.frame", "seq": 1, "w": 10, "h": 20, "img": frame})
        assert active.until("error")["code"] == "not_allowed"
        active.phone.send({"t": "phone.features", "features": ALL_FEATURES})
        active.phone.send({"t": "phone.frame", "seq": 2, "w": 10, "h": 20, "img": frame})
        assert active.until("phone.frame.ack")["seq"] == 2
        assert rig.rec.of(Events.PHONE_FRAME)[-1]["jpeg"] == b"\xff\xd8x"
        rig.core.bridge.phone_send(rig.session, "tap", {"x": 0.5, "y": 0.5})
        assert wait_for(lambda: active.cmds and active.cmds[-1]["action"] == "tap", 5)
    finally:
        active.stop()


def test_public_addresses_are_refused_unless_allowed(rig):  # noqa: F811
    bridge = rig.core.bridge
    assert bridge._address_allowed("192.168.1.20") and bridge._address_allowed("100.101.102.103")
    assert bridge._address_allowed("127.0.0.1") and bridge._address_allowed("fd7a:115c:a1e0::1")
    assert bridge._address_allowed("::ffff:10.0.0.4")
    assert not bridge._address_allowed("8.8.8.8") and not bridge._address_allowed("2001:4860::1")
    assert not bridge._address_allowed("not-an-ip")
    rig.core.settings.update({"bridge": {"allow_public": True}})
    assert bridge._address_allowed("8.8.8.8")


def test_too_many_files_at_once_are_refused(rig, tmp_path):  # noqa: F811
    active, _ready = active_phone(rig, tmp=tmp_path / "in")
    try:
        for i in range(4):
            active.phone.send({"t": "file.begin", "id": f"m{i}", "name": f"{i}.bin", "size": 5})
        active.phone.send({"t": "file.begin", "id": "m4", "name": "4.bin", "size": 5})
        assert active.until("error")["code"] == "rate_limit"
    finally:
        active.stop()


def test_commands_spoken_on_the_phone_act_on_that_phone(rig):  # noqa: F811
    # "What the laptop may do" switches are all off: the owner's own requests on the phone still work.
    off = {k: False for k in ALL_FEATURES}
    active, _ready = active_phone(rig, features=off, answers={
        "open_app": (False, "No app called “notepad please” on the phone.", {}),
        "call": (True, "Calling Mom…", {})})
    try:
        active.phone.send({"t": "chat", "id": 1, "text": "call mom"})
        assert "Calling Mom" in active.until("answer")["text"]
        cmd = active.cmds[-1]
        assert cmd["action"] == "call" and cmd["args"] == {"who": "mom", "own": True}
        active.phone.send({"t": "chat", "id": 2, "text": "set an alarm for 6:30 am"})
        active.until("answer")
        assert active.cmds[-1]["action"] == "alarm" and active.cmds[-1]["args"]["hour"] == 6
        active.phone.send({"t": "chat", "id": 3, "text": "turn on the torch"})
        active.until("answer")
        assert active.cmds[-1]["args"] == {"on": True, "own": True}
        # not a phone app -> it falls through to the laptop's chain (laptop control is off, so it's refused there)
        active.phone.send({"t": "chat", "id": 4, "text": "open notepad please"})
        assert "switched off" in active.until("answer", timeout=15)["text"]
        # naming the laptop never goes to the phone
        n = len(active.cmds)
        active.phone.send({"t": "chat", "id": 5, "text": "pause the music on my laptop"})
        active.until("answer", timeout=15)
        assert len(active.cmds) == n
        events = [r["event"] for r in rig.core.db.query("SELECT event FROM device_events")]
        assert "phone_voice_command" in events
    finally:
        active.stop()


def test_new_phone_actions_from_the_laptop_need_the_phone_switch(rig):  # noqa: F811
    active, _ready = active_phone(rig, features={**ALL_FEATURES, "open_apps": False})
    try:
        assert "switched off" in rig.reply("set an alarm on my phone for 7 am")
        active.phone.send({"t": "phone.features", "features": ALL_FEATURES})
        assert wait_for(lambda: rig.core.bridge.phone_for(rig.session).phone.can("open_apps"), 5)
        rig.reply("set a timer on my phone for 10 minutes")
        assert active.cmds[-1]["action"] == "timer" and active.cmds[-1]["args"] == {"seconds": 600}
        assert "own" not in active.cmds[-1]["args"]
    finally:
        active.stop()


def test_dumboo_speaks_on_the_phone_with_the_laptop_voice(rig):  # noqa: F811
    active, _ready = active_phone(rig)
    try:
        active.phone.send({"t": "speak", "id": 9, "text": "Hello there. How are you?"})
        first = active.until("speech.audio")
        assert first["id"] == 9 and first["rate"] > 0 and base64.b64decode(first["pcm"])
        end = first
        while not end["last"]:
            end = active.until("speech.audio")
        assert end["seq"] >= 1
    finally:
        active.stop()


def test_the_phone_agenda_can_add_complete_and_delete(rig):  # noqa: F811
    active, _ready = active_phone(rig)
    try:
        active.phone.send({"t": "agenda.do", "action": "add_task", "title": "Buy flowers", "priority": "high",
                           "due": "2030-01-02T09:30"})
        agenda = active.until("agenda")
        task = next(t for t in agenda["tasks"] if t["title"] == "Buy flowers")
        assert task["priority"] == "high" and task["has_time"]
        active.phone.send({"t": "agenda.do", "action": "complete_task", "id": task["id"]})
        assert all(t["id"] != task["id"] for t in active.until("agenda")["tasks"])
        from datetime import datetime, timedelta

        soon = (datetime.now() + timedelta(days=2)).replace(hour=15, minute=0).isoformat(timespec="minutes")
        active.phone.send({"t": "agenda.do", "action": "add_event", "title": "Dentist",
                           "starts_at": soon, "reminder_minutes": 30})
        event = next(e for e in active.until("agenda")["events"] if e["title"] == "Dentist")
        active.phone.send({"t": "agenda.do", "action": "delete_event", "id": event["id"]})
        assert all(e["id"] != event["id"] for e in active.until("agenda")["events"])
        active.phone.send({"t": "agenda.do", "action": "add_task", "title": ""})
        assert active.until("agenda.error")["message"]
    finally:
        active.stop()


def test_the_phone_files_screen_lists_and_fetches_only_that_folder(rig, tmp_path):  # noqa: F811
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "report.pdf").write_bytes(b"%PDF-1.4 x")
    (tmp_path / "secret.txt").write_text("no")
    active, _ready = active_phone(rig, features={**ALL_FEATURES, "files": False}, tmp=folder)
    try:
        active.phone.send({"t": "files.list"})
        assert [f["name"] for f in active.until("files.list")["files"]] == ["report.pdf"]
        active.phone.send({"t": "files.get", "name": "../secret.txt"})
        assert active.until("files.error")["message"]
        active.phone.send({"t": "files.get", "name": "report.pdf"})     # works although "files" is off: it asked
        assert wait_for(lambda: any(bytes(v) == b"%PDF-1.4 x" for v in active.files.values()), 5)
    finally:
        active.stop()


def test_choices_are_read_out_and_picked_by_voice(rig):  # noqa: F811
    choices = [{"id": "Rahul Sharma|+911", "label": "Rahul Sharma"}, {"id": "Rahul Office|+912", "label": "Rahul Office"}]
    active, _ready = active_phone(rig)

    def answers(cmd):
        if cmd["args"].get("pick"):
            return True, f"Calling {cmd['args']['pick'].split('|')[0]}…", {}
        return False, "I found 2 contacts called Rahul.", {"choices": choices}

    active.answers = answers
    try:
        ready = rig.say("call rahul on my phone")
        assert "1. Rahul Sharma" in ready["text"] and ready.get("listen_after")
        assert "Rahul Office" in rig.reply("the second one")
        assert active.cmds[-1]["args"]["pick"] == "Rahul Office|+912"
        rig.say("call rahul on my phone")
        assert "cancelled" in rig.reply("never mind")
    finally:
        active.stop()


def test_songs_are_looked_up_so_the_phone_plays_them_directly(paths):
    from tests.test_history_memory import Rig

    r = Rig(paths, music_lookup=lambda q: "dQw4w9WgXcQ" if "arijit" in q else None,
            bridge_overrides={"identity": {"protect": lambda b: b, "unprotect": lambda b: b},
                              "bridge": {"addresses": lambda: ["127.0.0.1", "100.101.102.103"], "host": "127.0.0.1"}})
    from tests.test_bridge import free_port

    r.core.settings.update({"bridge": {"port": free_port()}})
    try:
        active, ready = active_phone(r)
        assert ready["addresses"] == ["127.0.0.1", "100.101.102.103"] and ready["tailscale"] is True
        r.reply("play arijit singh on my phone")
        assert active.cmds[-1]["args"] == {"query": "arijit singh", "video": "dQw4w9WgXcQ"}
        active.stop()
    finally:
        r.core.bridge.stop()
        r.close()



def test_what_the_owner_sends_from_the_phone_needs_no_laptop_switch(rig, tmp_path):  # noqa: F811
    off = {k: False for k in ALL_FEATURES}
    active, _ready = active_phone(rig, features=off, tmp=tmp_path / "in")
    try:
        active.phone.send({"t": "clip.push", "text": "not allowed"})
        active.phone.send({"t": "clip.push", "text": "sent by me", "own": True})
        assert wait_for(lambda: rig.core.desktop.get_clipboard() == "sent by me", 5)
        active.phone.send({"t": "file.begin", "id": "o1", "name": "photo.jpg", "size": 3, "own": True})
        active.phone.send({"t": "file.chunk", "id": "o1", "data": base64.b64encode(b"abc").decode()})
        active.phone.send({"t": "file.end", "id": "o1"})
        assert active.until("file.saved")["name"] == "photo.jpg"
        active.phone.send({"t": "file.begin", "id": "o2", "name": "x.bin", "size": 3})
        assert active.until("error")["code"] == "not_allowed"
    finally:
        active.stop()
