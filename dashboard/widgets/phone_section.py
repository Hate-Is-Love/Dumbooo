"""ACCOUNT page → Phone: pair DUMBOO Mobile with a QR code, see paired phones, remove them, switch the link off."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton

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
        self.devices = QListWidget()
        self.devices.setMaximumHeight(120)
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
                      "Remove a phone here to cut it off at once. Controlling the laptop from the phone stays off "
                      "until a later update.")
        note.setWordWrap(True)
        note.setObjectName("HudLabel")
        for w in (self.status, self.qr, self.code, self.pair, self.devices):
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

    def _toggle(self) -> None:
        self._c.run_async(lambda: self._c.core.bridge.set_enabled(False), lambda _r: self.refresh())

    def hideEvent(self, event) -> None:
        if self._timer.isActive():
            self._stop_pairing("Pairing was closed.")
        super().hideEvent(event)
