"""The laptop's Phone panel (DUMBOO Mobile Phases 3–4): control the paired phone from the laptop.

Tabs: Phone (status, find my phone, music, call, open app) · Notifications (read and reply) · Files & clipboard ·
Screen (the phone's screen: click = tap, drag = swipe, Back/Home/Recents, typing). Every action goes through the
phone link, and the phone only does what its owner switched on in the app.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPushButton, QTabWidget, QVBoxLayout, QWidget)

from dashboard.widgets.hud import hud_label, hud_value

FEATURE_NAMES = {"notifications": "Notifications", "clipboard": "Clipboard", "files": "Files", "calls": "Calls",
                 "music": "Music", "find_phone": "Find my phone", "open_apps": "Open apps", "screen": "Screen sharing"}


class PhoneScreen(QLabel):
    """Shows the phone's screen; a click is a tap, a drag is a swipe, a long press is a long tap."""

    def __init__(self, act) -> None:
        super().__init__("Phone screen not shared yet")
        self._act = act
        self._pix: QPixmap | None = None
        self._down: tuple[QPoint, float] | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(300, 520)
        self.setStyleSheet("background: #050c0e; border: 1px solid #14454c;")

    def set_frame(self, pixmap: QPixmap) -> None:
        self._pix = pixmap
        self.setPixmap(pixmap.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation))

    def _norm(self, pos: QPoint) -> tuple[float, float] | None:
        shown = self.pixmap()
        if self._pix is None or shown is None or shown.isNull():
            return None
        w, h = shown.width(), shown.height()
        left, top = (self.width() - w) / 2, (self.height() - h) / 2
        x, y = (pos.x() - left) / w, (pos.y() - top) / h
        return (x, y) if 0 <= x <= 1 and 0 <= y <= 1 else None

    def mousePressEvent(self, event) -> None:
        self._down = (event.position().toPoint(), time.monotonic())

    def mouseReleaseEvent(self, event) -> None:
        if self._down is None:
            return
        start, t0 = self._down
        self._down = None
        a, b = self._norm(start), self._norm(event.position().toPoint())
        if a is None or b is None:
            return
        held_ms = int((time.monotonic() - t0) * 1000)
        if abs(a[0] - b[0]) < 0.02 and abs(a[1] - b[1]) < 0.02:
            self._act("tap", {"x": a[0], "y": a[1], "long": held_ms > 600})
        else:
            self._act("swipe", {"x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1],
                                "ms": max(100, min(1500, held_ms))})


class PhonePanel(QDialog):
    def __init__(self, controller, parent=None) -> None:
        super().__init__(parent)
        self._c = controller
        self.setWindowTitle("DUMBOO · Phone")
        self.resize(760, 760)
        layout = QVBoxLayout(self)
        self.header = hud_value()
        self.header.setWordWrap(True)
        layout.addWidget(self.header)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.tabs.addTab(self._build_phone(), "PHONE")
        self.tabs.addTab(self._build_notifications(), "NOTIFICATIONS")
        self.tabs.addTab(self._build_files(), "FILES & CLIPBOARD")
        self.tabs.addTab(self._build_screen(), "SCREEN")
        self._features: dict = {}
        self._status: dict = {}

    # -- plumbing ---------------------------------------------------------------------------------------------------
    def _request(self, action: str, args: dict | None = None, done=None, timeout: float = 12.0) -> None:
        session = self._c.session
        if session is None:
            return
        self.message.setText("…")

        def ok(result) -> None:
            self.message.setText("✓ " + (result.get("message") or "Done."))
            if done:
                done(result)

        def failed(exc) -> None:
            self.message.setText("⚠ " + (getattr(exc, "message", None) or str(exc)))

        self._c.run_async(lambda: self._c.core.bridge.phone_request(session, action, args, timeout), ok,
                          on_exception=failed)

    def _send(self, action: str, args: dict) -> None:
        session = self._c.session
        try:
            self._c.core.bridge.phone_send(session, action, args)
        except Exception as exc:
            self.message.setText("⚠ " + (getattr(exc, "message", None) or str(exc)))

    def open_tab(self, name: str) -> None:
        index = {"phone": 0, "notifications": 1, "files": 2, "screen": 3}.get(name, 0)
        self.tabs.setCurrentIndex(index)
        self.refresh()
        self.show()
        self.raise_()
        self.activateWindow()
        if name == "screen":
            self._start_screen()

    def refresh(self) -> None:
        session = self._c.session
        link = self._c.core.bridge.phone_for(session) if session is not None else None
        if link is None:
            self.header.setText("📱 Phone not connected. Open DUMBOO Mobile on your phone.")
            return
        self._features, self._status = dict(link.phone.features), dict(link.phone.status)
        on = [FEATURE_NAMES[k] for k, v in self._features.items() if v and k in FEATURE_NAMES]
        battery = self._status.get("battery")
        self.header.setText(f"📱 {link.device.name} · connected" + (f" · battery {battery}%" if battery is not None
                                                                     else "") +
                            f"\nAllowed on the phone: {', '.join(on) if on else 'nothing yet (DUMBOO Mobile → Settings)'}")
        self.notes.clear()
        for n in link.phone.notifications:
            item = QListWidgetItem(f"{n['at']} · {n['app']} · {n['title']}: {n['text']}"[:200])
            item.setData(Qt.ItemDataRole.UserRole, n["key"])
            self.notes.addItem(item)

    # -- tabs --------------------------------------------------------------------------------------------------------
    def _build_phone(self) -> QWidget:
        w = QWidget()
        grid = QGridLayout(w)
        find = QPushButton("🔔  FIND MY PHONE (RING)")
        find.setObjectName("Primary")
        find.clicked.connect(lambda: self._request("ring"))
        stop = QPushButton("STOP RINGING")
        stop.clicked.connect(lambda: self._request("stop_ring"))
        grid.addWidget(find, 0, 0)
        grid.addWidget(stop, 0, 1)
        grid.addWidget(hud_label("Music"), 1, 0, 1, 2)
        media = QHBoxLayout()
        for label, key in (("⏮", "previous"), ("⏯", "playpause"), ("⏭", "next")):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, k=key: self._request("media", {"key": k}))
            media.addWidget(b)
        for label, direction in (("🔉", "down"), ("🔊", "up"), ("🔇", "mute")):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, d=direction: self._request("volume", {"direction": d}))
            media.addWidget(b)
        grid.addLayout(media, 2, 0, 1, 2)
        self.play_query = QLineEdit()
        self.play_query.setPlaceholderText("play a song, artist or playlist on the phone")
        play = QPushButton("PLAY")
        play.clicked.connect(lambda: self.play_query.text() and self._request("play", {"query": self.play_query.text()}))
        grid.addWidget(self.play_query, 3, 0)
        grid.addWidget(play, 3, 1)
        grid.addWidget(hud_label("Call"), 4, 0, 1, 2)
        self.callee = QLineEdit()
        self.callee.setPlaceholderText("contact name or number")
        call = QPushButton("📞  CALL")
        call.clicked.connect(self._call)
        grid.addWidget(self.callee, 5, 0)
        grid.addWidget(call, 5, 1)
        grid.addWidget(hud_label("Open an app on the phone"), 6, 0, 1, 2)
        self.app_name = QLineEdit()
        self.app_name.setPlaceholderText("e.g. WhatsApp, YouTube, Camera")
        open_app = QPushButton("OPEN")
        open_app.clicked.connect(lambda: self.app_name.text() and self._request("open_app", {"name": self.app_name.text()}))
        grid.addWidget(self.app_name, 7, 0)
        grid.addWidget(open_app, 7, 1)
        refresh = QPushButton("REFRESH STATUS")
        refresh.clicked.connect(lambda: self._request("status", done=lambda _r: self.refresh()))
        grid.addWidget(refresh, 8, 0)
        grid.setRowStretch(9, 1)
        return w

    def _call(self) -> None:
        who = self.callee.text().strip()
        if not who:
            return
        number = who.replace(" ", "").replace("-", "")
        is_number = number.lstrip("+").isdigit()
        self._request("dial" if is_number else "call", {"who": number if is_number else who})

    def _build_notifications(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.notes = QListWidget()
        v.addWidget(self.notes, 1)
        row = QHBoxLayout()
        self.reply_text = QLineEdit()
        self.reply_text.setPlaceholderText("reply to the selected message")
        reply = QPushButton("REPLY")
        reply.setObjectName("Primary")
        reply.clicked.connect(self._reply)
        dismiss = QPushButton("DISMISS")
        dismiss.clicked.connect(self._dismiss)
        row.addWidget(self.reply_text, 1)
        row.addWidget(reply)
        row.addWidget(dismiss)
        v.addLayout(row)
        note = QLabel("Your phone's notifications appear here while it's connected and “Notifications” is allowed in "
                      "the app. Replying works for messages that offer a reply on the phone (WhatsApp, SMS, …).")
        note.setWordWrap(True)
        note.setObjectName("HudLabel")
        v.addWidget(note)
        return w

    def _selected_note(self) -> str | None:
        item = self.notes.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _reply(self) -> None:
        key, text = self._selected_note(), self.reply_text.text().strip()
        if key and text:
            self._request("reply", {"key": key, "text": text}, done=lambda _r: self.reply_text.clear())

    def _dismiss(self) -> None:
        key = self._selected_note()
        if key:
            self._request("dismiss", {"key": key}, done=lambda _r: self.refresh())

    def _build_files(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        send = QPushButton("📤  SEND A FILE TO THE PHONE…")
        send.setObjectName("Primary")
        send.clicked.connect(self._send_file)
        clip = QPushButton("📋  SEND THE LAPTOP'S CLIPBOARD TO THE PHONE")
        clip.clicked.connect(self._send_clipboard)
        v.addWidget(send)
        v.addWidget(clip)
        v.addWidget(hud_label("Received from the phone"))
        self.received = QListWidget()
        v.addWidget(self.received, 1)
        note = QLabel("Files sent from the phone (Share → DUMBOO laptop) are saved in Downloads\\DUMBOO Mobile. "
                      "Text copied with “Send clipboard” in the app is placed on the laptop's clipboard: press Ctrl+V.")
        note.setWordWrap(True)
        note.setObjectName("HudLabel")
        v.addWidget(note)
        return w

    def _send_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Send a file to the phone")
        if not path:
            return
        session = self._c.session
        self.message.setText("Sending…")
        self._c.run_async(lambda: self._c.core.bridge.send_file_to_phone(session, path),
                          lambda r: self.message.setText("✓ " + (r.get("message") or "Sent to the phone's Downloads.")),
                          on_exception=lambda exc: self.message.setText("⚠ " + (getattr(exc, "message", None) or str(exc))))

    def _send_clipboard(self) -> None:
        text = self._c.core.desktop.get_clipboard()
        if not text:
            self.message.setText("⚠ The laptop's clipboard is empty (or isn't text).")
            return
        self._request("clipboard.set", {"text": text[:20000]})

    def add_received(self, text: str) -> None:
        self.received.insertItem(0, f"{time.strftime('%H:%M')} · {text}")

    def _build_screen(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        top = QHBoxLayout()
        start = QPushButton("▶  SHARE PHONE SCREEN")
        start.setObjectName("Primary")
        start.clicked.connect(self._start_screen)
        stop = QPushButton("■  STOP")
        stop.clicked.connect(lambda: self._request("screen.stop"))
        top.addWidget(start)
        top.addWidget(stop)
        top.addStretch(1)
        v.addLayout(top)
        self.screen = PhoneScreen(self._send)
        v.addWidget(self.screen, 1)
        keys = QHBoxLayout()
        for label, action in (("◀ BACK", "back"), ("● HOME", "home"), ("▢ RECENTS", "recents"),
                              ("🔔", "notifications"), ("🔒", "lock")):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, a=action: self._send("global", {"action": a}))
            keys.addWidget(b)
        v.addLayout(keys)
        row = QHBoxLayout()
        self.phone_text = QLineEdit()
        self.phone_text.setPlaceholderText("type into the selected text box on the phone")
        typ = QPushButton("TYPE")
        typ.clicked.connect(lambda: self.phone_text.text() and (self._send("type", {"text": self.phone_text.text()}),
                                                                 self.phone_text.clear()))
        row.addWidget(self.phone_text, 1)
        row.addWidget(typ)
        v.addLayout(row)
        note = QLabel("Click to tap, drag to swipe, hold to long-press. The phone asks its owner to accept every "
                      "screen-sharing session, and shows that it's being shared.")
        note.setWordWrap(True)
        note.setObjectName("HudLabel")
        v.addWidget(note)
        return w

    def _start_screen(self) -> None:
        self.screen.setText("Waiting… accept the screen-sharing prompt on your phone.")
        self._request("screen.start", timeout=90)

    def show_frame(self, jpeg: bytes) -> None:
        pix = QPixmap()
        if pix.loadFromData(jpeg, "JPG"):
            self.screen.set_frame(pix)
