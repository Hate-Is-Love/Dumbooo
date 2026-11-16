"""ACCOUNT page → Phone: pair DUMBOO Mobile with a QR code, see paired phones, remove them, switch the link off."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton)

from app.event_bus import Events
from dashboard.widgets.hud import HudPanel, hud_value


def qr_pixmap(text: str, size: int = 230) -> QPixmap:
    """Draw ``text`` as a QR code (black on white, with a quiet zone) for the phone camera."""
    import qrcode

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=3)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    cells = len(matrix)
    scale = max(2, size // cells)
    image = QImage(cells * scale, cells * scale, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("black"))
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                painter.drawRect(x * scale, y * scale, scale, scale)
    painter.end()
    return QPixmap.fromImage(image)


class PhoneSection(HudPanel):
    def __init__(self, controller) -> None:
        super().__init__("Phone (DUMBOO Mobile)")
        self._c = controller
        self.setMinimumWidth(300)
        self.status = hud_value()
        self.status.setWordWrap(True)
        self.qr = QLabel()
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr.hide()
        self.code = QLabel("")
        self.code.setObjectName("HudValue")
        self.code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code.setWordWrap(True)
        self.code.hide()
        self.pair = QPushButton("◉  PAIR A PHONE")
        self.pair.setObjectName("Primary")
        self.pair.clicked.connect(self._pair)
        self.panel = QPushButton("📱  OPEN PHONE PANEL")
        self.panel.setToolTip("Control your phone from the laptop: ring it, music, calls, notifications, files, "
                              "clipboard and its screen.")
        self.panel.clicked.connect(lambda: self._c.core.bus.publish(Events.PHONE_PANEL, {"tab": "phone"}))
        self.devices = QListWidget()
        self.devices.setMaximumHeight(120)
        self.devices.currentItemChanged.connect(lambda *_: self._sync_control())
        self.control = QCheckBox("Allow laptop control from this phone")
        self.control.setToolTip("Mouse, keyboard, the laptop screen and DUMBOO's laptop commands from the phone. "
                                "Needs your password to switch on.")
        self.control.clicked.connect(self._control_clicked)
        self.control.setEnabled(False)
        self._caps: dict[int, dict] = {}
        self.remove = QPushButton("REMOVE PHONE")
        self.remove.setObjectName("Danger")
        self.remove.clicked.connect(self._remove)
        self.toggle = QPushButton("TURN PHONE LINK OFF")
        self.toggle.clicked.connect(self._toggle)
        row = QHBoxLayout()
        row.addWidget(self.remove)
        row.addWidget(self.toggle)
        note = QLabel("Install DUMBOO Mobile on your Android phone, tap “Pair with laptop” and scan the code. Only a "
                      "phone that scans it can connect: the link is encrypted and locked to that phone's own key. "
                      "Remove a phone here to cut it off at once. Laptop control (screen, mouse, keyboard, commands) "
                      "is off until you allow it for a phone; you'll see a notice whenever it's used. Away from home? Install "
                      "Tailscale on both devices and add the laptop's 100.x address in the app (README 22.10).")
        note.setWordWrap(True)
        note.setObjectName("HudLabel")
        for w in (self.status, self.qr, self.code, self.pair, self.panel, self.devices, self.control):
            self.body.addWidget(w)
        self.body.addLayout(row)
        self.body.addWidget(note)
        self.body.addStretch(1)
        self._left = 0
        self._known: set[int] = set()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # -- view ---------------------------------------------------------------------------------------------------------
    def refresh(self) -> None:
        session = self._c.session
        if session is None:
            return
        bridge, registry = self._c.core.bridge, self._c.core.devices
        self._c.run_async(lambda: (bridge.status(), registry.list(session)), self._show,
                          on_exception=lambda exc: self.status.setText(f"Phone link unavailable: {exc}"))

    def _show(self, result) -> None:
        status, devices = result
        if status["running"]:
            where = ", ".join(status["addresses"]) or "no network address"
            text = f"● Link on · port {status['port']} · {where}"
        elif status["enabled"] and status["error"]:
            text = f"⚠ Link couldn't start: {status['error']}"
        else:
            text = "Link off. It switches on when you pair a phone."
        self.status.setText(text)
        self.toggle.setVisible(status["running"])
        self.devices.clear()
        for d in devices:
            online = " · connected" if d["id"] in status["connected"] else ""
            seen = (d["last_seen_at"] or "")[:16].replace("T", " ")
            item = QListWidgetItem(f"{d['name']}{online}" + (f" · last seen {seen}" if seen and not online else ""))
            item.setData(Qt.ItemDataRole.UserRole, d["id"])
            self.devices.addItem(item)
        if not devices:
            placeholder = QListWidgetItem("No phones paired yet")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.devices.addItem(placeholder)
        self._caps = {d["id"]: d["capabilities"] for d in devices}
        if devices and self.devices.currentItem() is None:
            self.devices.setCurrentRow(0)
        self._sync_control()
        new = {d["id"] for d in devices} - self._known
        if self._timer.isActive() and new and self._known is not None:
            name = next(d["name"] for d in devices if d["id"] in new)
            self._stop_pairing(f"✓ Paired with {name}. You can talk to DUMBOO from your phone now.")
        self._known = {d["id"] for d in devices}
        self.remove.setEnabled(bool(devices))

    # -- pairing -------------------------------------------------------------------------------------------------------
    def _pair(self) -> None:
        session = self._c.session
        if session is None:
            return
        self.pair.setEnabled(False)

        def shown(offer) -> None:
            self.qr.setPixmap(qr_pixmap(offer.uri))
            self.qr.show()
            self._left = offer.expires_in_s
            self._code = offer.code
            self.code.show()
            self._tick()
            self._timer.start(1000)

        def failed(exc) -> None:
            self.pair.setEnabled(True)
            QMessageBox.warning(self, "Phone", getattr(exc, "message", None) or str(exc))

        self._c.run_async(lambda: self._c.core.bridge.offer_pairing(session), shown, on_exception=failed)

    def _tick(self) -> None:
        if self._left <= 0:
            self._stop_pairing("The code expired. Click “Pair a phone” for a new one.")
            return
        self.code.setText(f"Scan with DUMBOO Mobile · or type {self._code}\nvalid for {self._left} s")
        self._left -= 1
        if self._left % 2 == 0:
            self.refresh()                     # notices the new phone as soon as it pairs

    def _stop_pairing(self, message: str) -> None:
        self._timer.stop()
        self.qr.hide()
        self.code.setText(message)
        self.pair.setEnabled(True)
        session = self._c.session
        if session is not None:
            try:
                self._c.core.devices.cancel_pairing(session)
            except Exception:
                pass   # signed out or shutting down: the code expires on its own within 2 minutes

    # -- management ---------------------------------------------------------------------------------------------------
    def _remove(self) -> None:
        item = self.devices.currentItem()
        device_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        if device_id is None:
            QMessageBox.information(self, "Phone", "Select a phone in the list first.")
            return
        if QMessageBox.question(self, "Remove phone", f"Remove “{item.text().split(' · ')[0]}”? It is disconnected at "
                                                      "once and has to be paired again to connect.") \
                != QMessageBox.StandardButton.Yes:
            return
        session = self._c.session
        self._c.run_async(lambda: self._c.core.devices.revoke(session, device_id), lambda _r: self.refresh())

    def _selected(self) -> int | None:
        item = self.devices.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _sync_control(self) -> None:
        device_id = self._selected()
        caps = self._caps.get(device_id) if device_id is not None else None
        self.control.setEnabled(caps is not None)
        self.control.setChecked(bool(caps and caps.get("laptop_control")))

    def _control_clicked(self, checked: bool) -> None:
        device_id, session = self._selected(), self._c.session
        if device_id is None or session is None:
            return
        password = None
        if checked:
            password, ok = QInputDialog.getText(
                self, "Allow laptop control",
                "This phone will be able to see the screen and use the mouse, keyboard and DUMBOO's laptop commands.\n"
                "Enter your DUMBOO password to allow it:", QLineEdit.EchoMode.Password)
            if not ok or not password:
                self.control.setChecked(False)
                return

        def failed(exc) -> None:
            self.control.setChecked(not checked)
            QMessageBox.warning(self, "Phone", getattr(exc, "user_message", None) or getattr(exc, "message", None)
                                or str(exc))

        self._c.run_async(lambda: self._c.core.devices.set_capability(session, device_id, "laptop_control", checked,
                                                                      password),
                          lambda _caps: self.refresh(), on_exception=failed)

    def _toggle(self) -> None:
        self._c.run_async(lambda: self._c.core.bridge.set_enabled(False), lambda _r: self.refresh())

    def hideEvent(self, event) -> None:
        if self._timer.isActive():
            self._stop_pairing("Pairing was closed.")
        super().hideEvent(event)
