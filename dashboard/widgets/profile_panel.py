"""ACCOUNT page: the signed-in user's profile, preferences, password, own data and sign-out."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout, QWidget)

from dashboard.styles import theme
from dashboard.widgets.activity import ScanIndicator
from dashboard.widgets.hud import HudPanel, hud_label, hud_value
from dashboard.widgets.identity_scanner import IdentityScanner
from dashboard.widgets.login_page import password_field
from services.database_status import database_status


class ProfilePanel(QWidget):
    preferences_saved = Signal()

    def __init__(self, controller) -> None:
        super().__init__()
        self._c = controller
        core = controller.core
        self._auth, self._profile, self._history = core.auth, core.profile, core.history
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        self.activity = ScanIndicator("PROFILE LINK READY")
        layout.addWidget(self.activity)
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)
        layout.addLayout(grid, 1)

        identity = HudPanel("Identity")
        form = QGridLayout()
        self.username = hud_value()
        self.role = hud_value()
        self.permissions = hud_value()
        self.permissions.setWordWrap(True)
        self.display_name = QLineEdit()
        self.display_name.setMaxLength(60)
        save_name = QPushButton("SAVE NAME")
        save_name.clicked.connect(self._save_name)
        form.addWidget(hud_label("Username"), 0, 0)
        form.addWidget(self.username, 0, 1)
        form.addWidget(hud_label("Role / status"), 1, 0)
        form.addWidget(self.role, 1, 1)
        form.addWidget(hud_label("Display name"), 2, 0)
        name_row = QHBoxLayout()
        name_row.addWidget(self.display_name, 1)
        name_row.addWidget(save_name)
        form.addLayout(name_row, 2, 1)
        form.addWidget(hud_label("Permissions"), 3, 0)
        form.addWidget(self.permissions, 3, 1)
        identity.body.addLayout(form)
        logout = QPushButton("⏏  SIGN OUT")
        logout.setObjectName("Danger")
        logout.clicked.connect(self._c.logout)
        identity.body.addWidget(logout)
        identity.body.addStretch(1)
        grid.addWidget(identity, 0, 0)

        prefs = HudPanel("Preferences")
        pform = QFormLayout()
        self.title = QLineEdit()
        self.title.setMaxLength(40)
        self.title.setToolTip("How DUMBOO addresses you, e.g. Sir, Madam, Boss or your name")
        self.response_length = QComboBox()
        self.response_length.addItems(["short", "detailed"])
        self.voice_output = QComboBox()
        self.voice_output.addItems(["on", "off"])
        self.greeting = QComboBox()
        self.greeting.addItems(["on", "off"])
        self.greeting.setToolTip("After you sign in, DUMBOO greets you (and introduces itself the first time).")
        save_prefs = QPushButton("SAVE PREFERENCES")
        save_prefs.setObjectName("Primary")
        save_prefs.clicked.connect(self._save_prefs)
        pform.addRow("Address me as", self.title)
        pform.addRow("Answer length", self.response_length)
        pform.addRow("Speak answers", self.voice_output)
        pform.addRow("Greet me after sign-in", self.greeting)
        pform.addRow(save_prefs)
        prefs.body.addLayout(pform)
        prefs.body.addStretch(1)
        grid.addWidget(prefs, 0, 1)

        password = HudPanel("Change password")
        cform = QFormLayout()
        self.current = password_field("current password")
        self.new = password_field("new password (8+ characters)")
        self.confirm = password_field("repeat new password")
        change = QPushButton("CHANGE PASSWORD")
        change.clicked.connect(self._change_password)
        note = QLabel("Changing your password signs out your other remembered sessions.")
        note.setWordWrap(True)
        note.setObjectName("HudLabel")
        cform.addRow("Current", self.current)
        cform.addRow("New", self.new)
        cform.addRow("Confirm", self.confirm)
        cform.addRow(change)
        cform.addRow(note)
        password.body.addLayout(cform)
        grid.addWidget(password, 1, 0)

        data = HudPanel("Your data")
        self.data_view = QLabel("")
        self.data_view.setObjectName("HudValue")
        self.data_view.setWordWrap(True)
        export = QPushButton("EXPORT MY DATA…")
        export.clicked.connect(self._export)
        refresh = QPushButton("REFRESH")
        refresh.clicked.connect(self.refresh)
        row = QHBoxLayout()
        row.addWidget(export)
        row.addWidget(refresh)
        row.addStretch(1)
        data.body.addWidget(self.data_view)
        data.body.addLayout(row)
        data.body.addStretch(1)
        grid.addWidget(data, 1, 1)
        grid.addWidget(self._build_face(), 0, 2, 2, 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    # -- face unlock --------------------------------------------------------------------------
    def _build_face(self) -> HudPanel:
        face = HudPanel("Face unlock")
        face.setMinimumWidth(300)
        self.face_status = hud_value()
        self.face_status.setWordWrap(True)
        self.face_scanner = IdentityScanner()
        self.face_scanner.setMinimumSize(260, 260)
        self.face_scanner.hide()
        self.face_pass = password_field("your password, to confirm it's you")
        self.face_setup = QPushButton("◉  SET UP FACE UNLOCK")
        self.face_setup.setObjectName("Primary")
        self.face_setup.clicked.connect(self._face_enroll)
        self.face_stop = QPushButton("CANCEL")
        self.face_stop.clicked.connect(lambda: self._face_worker and self._face_worker.cancel())
        self.face_stop.hide()
        self.face_remove = QPushButton("REMOVE FACE DATA")
        self.face_remove.setObjectName("Danger")
        self.face_remove.clicked.connect(self._face_delete)
        self._face_worker = None
        note = QLabel("Look at the camera, then turn your head slightly to each side when asked. Only face "
                      "measurements are stored, encrypted for your Windows account, never pictures. The camera is "
                      "on only during setup and face sign-in. Your password always works too.")
        note.setWordWrap(True)
        note.setObjectName("HudLabel")
        face.body.addWidget(self.face_status)
        face.body.addWidget(self.face_scanner, 1)
        face.body.addWidget(self.face_pass)
        face.body.addWidget(self.face_setup)
        face.body.addWidget(self.face_stop)
        face.body.addWidget(self.face_remove)
        face.body.addWidget(note)
        face.body.addStretch(1)
        return face

    def _face_show(self, status: dict) -> None:
        engine = self._c.core.face_engine
        if not engine.available:
            text = f"Not available: {engine.error}."
        elif not status["enabled"]:
            text = "Switched off by the owner (ADMIN page)."
        elif status["enrolled"]:
            text = f"✓ Set up ({status['samples']} samples, {status['updated_at'][:10]}). Use SIGN IN WITH FACE."
        else:
            text = "Not set up yet."
        self.face_status.setText(text)
        usable = engine.available and status["enabled"]
        self.face_setup.setText("◉  SET UP AGAIN" if status["enrolled"] else "◉  SET UP FACE UNLOCK")
        self.face_setup.setEnabled(usable)
        self.face_remove.setVisible(status["enrolled"])

    def _face_enroll(self) -> None:
        password = self.face_pass.text()
        if not password:
            self._error("Enter your password first, to confirm it's you.")
            return
        session = self._c.session
        core = self._c.core
        # Check the password before the camera goes on.
        self._c.track(self.activity, "CONFIRMING IDENTITY", lambda: core.auth.reauthenticate(session, password),
                      lambda _r: self._face_camera(password), lambda msg: (self.face_pass.clear(), self._error(msg)),
                      done_label="IDENTITY CONFIRMED")

    def _face_camera(self, password: str) -> None:
        from dashboard.face_session import FaceSessionWorker
        from security.face import FaceEnrollment

        core = self._c.core
        machine = FaceEnrollment()
        worker = FaceSessionWorker(core.face_engine, machine, core.camera_factory)
        worker.progress.connect(lambda u: (self.face_scanner.set_camera(u["image"], u["box"], u["step"], u["steps"]),
                                           self.face_status.setText(u["message"]), self._c.keep_awake()))
        worker.finished.connect(lambda result: self._face_captured(result, machine, password))
        self._face_worker = worker
        self.face_scanner.set_mode("face", "face setup")
        self.face_scanner.show()
        self.face_setup.hide()
        self.face_stop.show()
        worker.start()

    def _face_captured(self, result, machine, password: str) -> None:
        self._face_worker = None
        self.face_scanner.set_camera(None)
        self.face_scanner.hide()
        self.face_stop.hide()
        self.face_setup.show()
        if not result.ok:
            self.face_pass.clear()
            self._error(result.message)
            self.refresh()
            return
        session, core = self._c.session, self._c.core
        vectors = machine.embeddings()
        self._c.track(self.activity, "SAVING FACE DATA", lambda: core.face.enroll(session, password, vectors),
                      lambda n: (self.face_pass.clear(),
                                 self._note(f"Face unlock is set up ({n} samples). Next time, choose SIGN IN WITH FACE."),
                                 self.refresh()),
                      lambda msg: (self.face_pass.clear(), self._error(msg)), done_label="FACE SAVED")

    def _face_delete(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        if QMessageBox.question(self, "Remove face data", "Remove your face unlock data? You can set it up again "
                                "any time.") != QMessageBox.StandardButton.Yes:
            return
        session, core = self._c.session, self._c.core
        self._c.track(self.activity, "REMOVING FACE DATA", lambda: core.face.remove(session),
                      lambda _r: (self._note("Face unlock data removed."), self.refresh()), self._error,
                      done_label="REMOVED")

    def hideEvent(self, event) -> None:
        if self._face_worker is not None:
            self._face_worker.cancel()  # never leave the camera on when the page goes away
        super().hideEvent(event)

    def _note(self, text: str, ok: bool = True) -> None:
        self.status.setStyleSheet(f"color: {theme.GREEN if ok else theme.RED};")
        self.status.setText(text)

    def _error(self, text: str) -> None:
        self._note(text, ok=False)

    def clear(self) -> None:
        for widget in (self.username, self.role, self.permissions, self.data_view, self.status):
            widget.setText("")
        for field in (self.display_name, self.title, self.current, self.new, self.confirm, self.face_pass):
            field.clear()

    # -- loading -------------------------------------------------------------------
    def refresh(self) -> None:
        session = self._c.session
        if session is None:
            return
        core = self._c.core

        def load():
            user = self._auth.require(session)
            return (user, self._profile.get(session), database_status(core.db, core.auth, session),
                    core.face.status(session))

        self._c.track(self.activity, "LOADING PROFILE", load, self._show, self._error, done_label="PROFILE LOADED")

    def _show(self, result) -> None:
        user, prefs, status, face = result
        self._face_show(face)
        self.greeting.setCurrentText(prefs.get("greeting", "on"))
        self.username.setText(user.username)
        self.role.setText(f"{user.role.upper()} · ACTIVE")
        self.permissions.setText(", ".join(sorted(user.permissions)))
        self.display_name.setText(user.display_name)
        self.title.setText(prefs["title"])
        self.response_length.setCurrentText(prefs["response_length"])
        self.voice_output.setCurrentText(prefs["voice_output"])
        mine = status["your data"]
        self.data_view.setText("\n".join(f"{k}: {v}" for k, v in mine.items()))

    # -- actions --------------------------------------------------------------------------
    def _save_name(self) -> None:
        session, name = self._c.session, self.display_name.text()
        self._c.track(self.activity, "WRITING PROFILE", lambda: self._auth.update_own_display_name(session, name),
                      lambda _r: (self._note("Display name saved."), self.refresh(),
                                  self._c.core.accounts.refresh_profile()), self._error, done_label="SAVED")

    def _save_prefs(self) -> None:
        session = self._c.session
        values = {"title": self.title.text(), "response_length": self.response_length.currentText(),
                  "voice_output": self.voice_output.currentText(), "greeting": self.greeting.currentText()}

        def work() -> None:
            for key, value in values.items():
                self._profile.set(session, key, value)

        def done(_r) -> None:
            self._note("Preferences saved.")
            self._c.core.accounts.refresh_profile()
            self.preferences_saved.emit()

        self._c.track(self.activity, "WRITING PREFERENCES", work, done, self._error, done_label="SAVED")

    def _change_password(self) -> None:
        if self.new.text() != self.confirm.text():
            self._error("The new passwords do not match.")
            return
        session, current, new = self._c.session, self.current.text(), self.new.text()
        for field in (self.current, self.new, self.confirm):
            field.clear()
        self._c.track(self.activity, "RE-KEYING CREDENTIALS", lambda: self._auth.change_password(session, current, new),
                      lambda _r: self._note("Password changed. Other sessions were signed out."), self._error,
                      done_label="PASSWORD UPDATED")

    def _export(self, path: str | None = None) -> None:
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, "Export my data", str(Path.home() / "dumboo-export.json"),
                                                  "JSON (*.json)")
        if not path:
            return
        session, recorder = self._c.session, self._c.core.recorder

        def work() -> int:
            recorder.flush()
            data = self._history.export_json(session)
            Path(path).write_text(data, encoding="utf-8")
            return len(data)

        self._c.track(self.activity, "EXPORTING", work,
                      lambda size: self._note(f"Exported {size / 1024:.1f} KB to {path}. Keep it private."), self._error,
                      done_label="EXPORT COMPLETE")
