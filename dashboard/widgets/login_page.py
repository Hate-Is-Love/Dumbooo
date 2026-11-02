"""Sign-in screen: owner setup (first run), sign in, optional registration, forced password change.

Password checks run on a worker thread (Argon2id takes ~0.1 s on purpose) while
the identity scanner animates; the result (granted / denied) is the real one.
Password fields are cleared after every attempt.
"""

from __future__ import annotations

from PySide6.QtCore import QElapsedTimer, Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QCheckBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QStackedWidget,
                               QVBoxLayout, QWidget)

from dashboard.styles import theme
from dashboard.widgets.hud import HudPanel
from dashboard.widgets.identity_scanner import IdentityScanner

MIN_SCAN_MS = 650  # keep the scan visible long enough to read, even when the check is faster


def password_field(placeholder: str) -> QLineEdit:
    field = QLineEdit()
    field.setEchoMode(QLineEdit.EchoMode.Password)
    field.setPlaceholderText(placeholder)
    field.setMaxLength(128)
    toggle = QAction("👁", field)
    toggle.setToolTip("Show / hide password")
    toggle.setCheckable(True)
    toggle.toggled.connect(lambda on: field.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
    field.addAction(toggle, QLineEdit.ActionPosition.TrailingPosition)
    field.visibility_toggle = toggle  # for tests
    return field


class LoginPage(QWidget):
    signed_in = Signal()

    def __init__(self, controller) -> None:
        super().__init__()
        self._c = controller
        self._accounts = controller.core.accounts
        self._auth = controller.core.auth
        self._clock = QElapsedTimer()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(30)
        self.scanner = IdentityScanner()
        layout.addWidget(self.scanner, 3)

        panel = HudPanel("Identification // secure access")
        panel.setFixedWidth(430)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setMinimumHeight(40)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_login())     # 0
        self.stack.addWidget(self._build_setup())     # 1
        self.stack.addWidget(self._build_register())  # 2
        self.stack.addWidget(self._build_change())    # 3
        panel.body.addWidget(self.stack)
        panel.body.addWidget(self.message)
        panel.body.addStretch(1)
        privacy = QLabel("Your conversations and memories are stored only on this PC and are visible only to your "
                         "account. The owner manages accounts but cannot read other users' private data.")
        privacy.setWordWrap(True)
        privacy.setObjectName("HudLabel")
        panel.body.addWidget(privacy)
        wrap = QVBoxLayout()
        wrap.addStretch(1)
        wrap.addWidget(panel)
        wrap.addStretch(1)
        layout.addLayout(wrap)
        self.refresh()

    # -- forms ---------------------------------------------------------------------
    def _build_login(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        title = QLabel("SIGN IN")
        title.setObjectName("Title")
        self.login_user = QLineEdit()
        self.login_user.setPlaceholderText("username")
        self.login_user.setMaxLength(32)
        self.login_pass = password_field("password")
        self.login_pass.returnPressed.connect(self._login)
        self.remember = QCheckBox("Stay signed in on this PC")
        self.remember.setToolTip("Keeps you signed in after restarts (encrypted with your Windows account). "
                                 "Leave off on shared computers.")
        self.login_button = QPushButton("SIGN IN")
        self.login_button.setObjectName("Primary")
        self.login_button.clicked.connect(self._login)
        self.face_button = QPushButton("◉  SIGN IN WITH FACE")
        self.face_button.setToolTip("Look at the camera, then turn your head slightly when asked. The camera is on only "
                                    "during the check; no pictures are saved.")
        self.face_button.clicked.connect(self._face_login)
        self.face_cancel = QPushButton("CANCEL FACE SCAN")
        self.face_cancel.clicked.connect(self._face_stop)
        self.face_cancel.hide()
        self._face_worker = None
        self.register_link = QPushButton("Create an account")
        self.register_link.setFlat(True)
        self.register_link.clicked.connect(lambda: self._show(2))
        form.addRow(title)
        form.addRow("Username", self.login_user)
        form.addRow("Password", self.login_pass)
        form.addRow(self.remember)
        form.addRow(self.login_button)
        form.addRow(self.face_button)
        form.addRow(self.face_cancel)
        form.addRow(self.register_link)
        return w

    def _build_setup(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        title = QLabel("OWNER SETUP")
        title.setObjectName("Title")
        intro = QLabel("First start: create the owner account. The owner manages other accounts and system "
                       "settings. There is no default password: choose your own (8+ characters).")
        intro.setWordWrap(True)
        self.setup_user = QLineEdit()
        self.setup_user.setPlaceholderText("e.g. roshan")
        self.setup_user.setMaxLength(32)
        self.setup_name = QLineEdit()
        self.setup_name.setPlaceholderText("e.g. Roshan")
        self.setup_title = QLineEdit("Sir")
        self.setup_title.setMaxLength(40)
        self.setup_pass = password_field("new password")
        self.setup_confirm = password_field("repeat password")
        self.setup_confirm.returnPressed.connect(self._setup)
        self.setup_button = QPushButton("INITIALISE OWNER")
        self.setup_button.setObjectName("Primary")
        self.setup_button.clicked.connect(self._setup)
        form.addRow(title)
        form.addRow(intro)
        form.addRow("Username", self.setup_user)
        form.addRow("Display name", self.setup_name)
        form.addRow("Address me as", self.setup_title)
        form.addRow("Password", self.setup_pass)
        form.addRow("Confirm", self.setup_confirm)
        form.addRow(self.setup_button)
        return w

    def _build_register(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        title = QLabel("CREATE ACCOUNT")
        title.setObjectName("Title")
        self.reg_user = QLineEdit()
        self.reg_user.setMaxLength(32)
        self.reg_name = QLineEdit()
        self.reg_pass = password_field("new password")
        self.reg_confirm = password_field("repeat password")
        create = QPushButton("CREATE ACCOUNT")
        create.setObjectName("Primary")
        create.clicked.connect(self._register)
        back = QPushButton("Back to sign in")
        back.setFlat(True)
        back.clicked.connect(lambda: self._show(0))
        form.addRow(title)
        form.addRow("Username", self.reg_user)
        form.addRow("Display name", self.reg_name)
        form.addRow("Password", self.reg_pass)
        form.addRow("Confirm", self.reg_confirm)
        form.addRow(create)
        form.addRow(back)
        return w

    def _build_change(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        title = QLabel("NEW PASSWORD REQUIRED")
        title.setObjectName("Title")
        intro = QLabel("Your password was set by the owner. Choose your own password to continue.")
        intro.setWordWrap(True)
        self.change_current = password_field("temporary password")
        self.change_new = password_field("new password")
        self.change_confirm = password_field("repeat new password")
        save = QPushButton("SET PASSWORD")
        save.setObjectName("Primary")
        save.clicked.connect(self._change_password)
        out = QPushButton("Sign out")
        out.setFlat(True)
        out.clicked.connect(self._c.logout)
        form.addRow(title)
        form.addRow(intro)
        form.addRow("Current", self.change_current)
        form.addRow("New", self.change_new)
        form.addRow("Confirm", self.change_confirm)
        form.addRow(save)
        form.addRow(out)
        return w

    # -- state ---------------------------------------------------------------------------
    def _show(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.message.setText("")

    def refresh(self, reason: str = "") -> None:
        """Pick the right form: owner setup on first run, otherwise sign in."""
        policy = self._c.core.settings.section("accounts")
        if self._accounts.needs_owner_setup():
            self._show(1)
            self.scanner.set_mode("setup", "no owner account yet")
        else:
            self._show(0)
            self.scanner.set_mode("idle", reason or "")
        self.remember.setVisible(policy["remember_me_allowed"] and self._accounts.remember_me_available())
        self.register_link.setVisible(policy["registration_enabled"])
        self.face_button.setVisible(self._face_ready() and self._face_worker is None)
        if reason:
            self._note(reason, ok=None)

    # -- face unlock ---------------------------------------------------------------------------
    def _face_ready(self) -> bool:
        core = self._c.core
        face, engine = getattr(core, "face", None), getattr(core, "face_engine", None)
        try:
            return bool(face and engine and face.any_enrolled() and engine.available)
        except Exception:
            return False

    def _face_login(self) -> None:
        from dashboard.dashboard_controller import friendly_message
        from dashboard.face_session import FaceSessionWorker
        from security.face import FaceSignIn

        core = self._c.core
        try:
            identify = core.face.matcher()
        except Exception as exc:
            self._note(friendly_message(exc), ok=False)
            return
        self._busy(True)
        self.face_button.hide()
        self.face_cancel.show()
        self.scanner.set_mode("face", "look at the camera")
        self._note("Look at the camera.", ok=None)
        worker = FaceSessionWorker(core.face_engine, FaceSignIn(identify), core.camera_factory)
        worker.progress.connect(self._face_progress)
        worker.finished.connect(self._face_done)
        self._face_worker = worker
        worker.start()

    def _face_progress(self, update: dict) -> None:
        if self._face_worker is None:
            return
        self._c.keep_awake()
        self.scanner.set_camera(update["image"], update["box"], update["step"], update["steps"])
        self.scanner.subtitle = update["message"]
        self._note(update["message"], ok=None)

    def _face_stop(self) -> None:
        if self._face_worker is not None:
            self._face_worker.cancel()

    def _face_done(self, result) -> None:
        from services.face_service import FaceError

        self._face_worker = None
        self.scanner.set_camera(None)
        self.face_cancel.hide()
        self.face_button.show()
        if result.ok and result.user_id is not None:
            self._clock.restart()
            self.scanner.set_mode("scanning", "face confirmed · opening your session")
            self._c.run_async(lambda: self._accounts.login_with_face(result.user_id, result.samples),
                              lambda _s: self._finish(self._granted),
                              on_exception=lambda exc: self._finish(lambda: self._denied(exc)))
            return
        if result.message != "Cancelled.":
            try:
                self._c.core.face.record_failure(result.stage)
            except Exception:
                pass
            self._denied(FaceError(result.message))
        else:
            self._busy(False)
            self.scanner.set_mode("idle")
            self._note("Face scan cancelled. Camera off.", ok=None)

    def hideEvent(self, event) -> None:
        self._face_stop()  # never leave the camera on when the page goes away
        super().hideEvent(event)

    def require_password_change(self) -> None:
        self._show(3)
        self.scanner.set_mode("setup", "password change required")

    def _note(self, text: str, ok: bool | None) -> None:
        color = theme.MUTED if ok is None else (theme.GREEN if ok else theme.RED)
        self.message.setStyleSheet(f"color: {color};")
        self.message.setText(text)

    def _busy(self, on: bool) -> None:
        for button in (self.login_button, self.setup_button):
            button.setEnabled(not on)

    def _finish(self, apply) -> None:
        """Apply a result, but not before the scan has been visible for MIN_SCAN_MS."""
        remaining = MIN_SCAN_MS - self._clock.elapsed()
        QTimer.singleShot(max(0, remaining), apply)

    # -- actions --------------------------------------------------------------------------
    def _login(self) -> None:
        username, password = self.login_user.text().strip(), self.login_pass.text()
        self.login_pass.clear()
        if not username or not password:
            self._note("Enter your username and password.", ok=False)
            return
        remember = self.remember.isVisible() and self.remember.isChecked()
        self._start_scan("checking credentials")
        self._c.run_async(lambda: self._accounts.login(username, password, remember),
                          lambda _s: self._finish(self._granted),
                          on_exception=lambda exc: self._finish(lambda: self._denied(exc)))

    def _setup(self) -> None:
        if self.setup_pass.text() != self.setup_confirm.text():
            self._note("The passwords do not match.", ok=False)
            return
        values = (self.setup_user.text().strip(), self.setup_name.text().strip(), self.setup_pass.text(),
                  self.setup_title.text().strip() or "Sir")
        self.setup_pass.clear()
        self.setup_confirm.clear()
        self._start_scan("creating owner account")
        self._c.run_async(lambda: self._accounts.setup_owner(*values), lambda _s: self._finish(self._granted),
                          on_exception=lambda exc: self._finish(lambda: self._denied(exc, setup=True)))

    def _register(self) -> None:
        if self.reg_pass.text() != self.reg_confirm.text():
            self._note("The passwords do not match.", ok=False)
            return
        values = (self.reg_user.text().strip(), self.reg_name.text().strip(), self.reg_pass.text())
        self.reg_pass.clear()
        self.reg_confirm.clear()
        self._start_scan("creating account")

        def done(_id) -> None:
            self.scanner.set_mode("idle", "account created")
            self._show(0)
            self.login_user.setText(values[0])
            self._note("Account created. Sign in with your new password.", ok=True)

        self._c.run_async(lambda: self._auth.register(*values), lambda r: self._finish(lambda: done(r)),
                          on_exception=lambda exc: self._finish(lambda: self._denied(exc)))

    def _change_password(self) -> None:
        if self.change_new.text() != self.change_confirm.text():
            self._note("The new passwords do not match.", ok=False)
            return
        current, new = self.change_current.text(), self.change_new.text()
        for field in (self.change_current, self.change_new, self.change_confirm):
            field.clear()
        self._start_scan("updating password")
        self._c.run_async(lambda: self._auth.change_password(self._c.session, current, new),
                          lambda _r: self._finish(self._password_changed),
                          on_exception=lambda exc: self._finish(lambda: self._denied(exc, keep_form=True)))

    def _start_scan(self, subtitle: str) -> None:
        self._clock.restart()
        self._busy(True)
        self.message.setText("")
        self.scanner.set_mode("scanning", subtitle)

    def _granted(self) -> None:
        self._busy(False)
        user = self._accounts.current_user()
        name = user.display_name if user else ""
        self.scanner.set_mode("granted", f"welcome, {name}" if name else "")
        self._note("Identity confirmed.", ok=True)
        if user and user.must_change_password:
            QTimer.singleShot(700, self.require_password_change)
            return
        QTimer.singleShot(800, self.signed_in.emit)

    def _password_changed(self) -> None:
        self._busy(False)
        self.scanner.set_mode("granted", "password updated")
        self._note("Password updated.", ok=True)
        QTimer.singleShot(700, self.signed_in.emit)

    def _denied(self, exc: BaseException, setup: bool = False, keep_form: bool = False) -> None:
        from dashboard.dashboard_controller import friendly_message

        self._busy(False)
        self.scanner.set_mode("denied", "identity not confirmed" if not setup else "setup not completed")
        self._note(friendly_message(exc), ok=False)
        QTimer.singleShot(1600, lambda: self.scanner.set_mode("setup" if (setup or keep_form) else "idle"))
