"""ADMIN page (owner only): accounts, permissions, security settings, database health.

Every button calls an ``AuthService`` method that checks the owner's session
itself (the page being hidden from normal users is a convenience, not the
protection). Sensitive actions ask for the owner's password again. Nothing on
this page can show another user's conversations or memories.
"""

from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from dashboard.styles import theme
from dashboard.widgets.activity import ScanIndicator
from dashboard.widgets.hud import HudPanel
from dashboard.widgets.login_page import password_field
from dashboard.widgets.reauth import run_sensitive
from services.auth_service import OWNER_ONLY, PERMISSIONS
from services.database_status import database_status


class CreateUserDialog(QDialog):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create user")
        form = QFormLayout(self)
        self.username = QLineEdit()
        self.username.setMaxLength(32)
        self.display = QLineEdit()
        self.password = password_field("leave empty to generate a temporary password")
        note = QLabel("The user must choose their own password at first sign-in.")
        note.setObjectName("HudLabel")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow("Username", self.username)
        form.addRow("Display name", self.display)
        form.addRow("Password", self.password)
        form.addRow(note)
        form.addRow(buttons)


class PermissionsDialog(QDialog):
    def __init__(self, parent: QWidget, user: dict) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Permissions · {user['username']}")
        layout = QVBoxLayout(self)
        self.boxes: dict[str, QCheckBox] = {}
        for permission, description in PERMISSIONS.items():
            box = QCheckBox(f"{description}  ({permission})")
            box.setChecked(permission in user["permissions"])
            if permission in OWNER_ONLY:
                box.setEnabled(False)
            self.boxes[permission] = box
            layout.addWidget(box)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def show_secret_once(parent: QWidget, title: str, text: str, secret: str) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(f"{text}\n\n{secret}\n\nIt is shown only this once. Give it to the user privately; "
                "they must replace it at first sign-in.")
    copy = box.addButton("Copy", QMessageBox.ButtonRole.ActionRole)
    box.addButton(QMessageBox.StandardButton.Ok)
    box.exec()
    if box.clickedButton() is copy:
        QGuiApplication.clipboard().setText(secret)


class AdminPanel(QWidget):
    COLUMNS = ["USERNAME", "DISPLAY NAME", "ROLE", "STATUS", "LAST SIGN-IN", "LOCKED", "MUST CHANGE PW"]
    SECURITY_FIELDS = [
        ("registration_enabled", "Allow self-registration", bool),
        ("session_timeout_min", "Sign out after inactivity (min)", (1, 1440)),
        ("lockout_threshold", "Lock after failed sign-ins", (3, 20)),
        ("lockout_minutes", "Lockout duration (min)", (1, 1440)),
        ("remember_me_allowed", "Allow 'stay signed in'", bool),
        ("remember_me_days", "'Stay signed in' lasts (days)", (1, 90)),
        ("history_retention_days", "Delete conversations older than (days, 0 = never)", (0, 3650)),
        ("memory_auto_extract", "Save clear personal facts automatically", bool),
        ("memory_ai_learning", "Learn preferences and interests from conversations (AI)", bool),
        ("face_unlock_enabled", "Allow signing in with face unlock", bool),
        ("retrieval_memories", "Memories given to the AI per question", (0, 20)),
        ("retrieval_messages", "Past messages given to the AI per question", (0, 20)),
        ("login_attempt_retention_days", "Keep sign-in attempts (days)", (1, 365)),
    ]

    def __init__(self, controller) -> None:
        super().__init__()
        self._c = controller
        self._auth = controller.core.auth
        self._users: list[dict] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        self.activity = ScanIndicator("ADMIN CONSOLE READY")
        layout.addWidget(self.activity)
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        layout.addLayout(grid, 1)

        users = HudPanel("Accounts")
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        users.body.addWidget(self.table, 1)
        row1, row2 = QHBoxLayout(), QHBoxLayout()
        buttons = [
            (row1, "+ CREATE USER", self._create, "Primary"), (row1, "EDIT NAME", self._edit_name, None),
            (row1, "PERMISSIONS", self._permissions, None), (row1, "RESET PASSWORD", self._reset, None),
            (row2, "DISABLE / REACTIVATE", self._toggle_status, None), (row2, "MAKE OWNER", self._transfer, None),
            (row2, "DELETE ACCOUNT", self._delete, "Danger"), (row2, "AUDIT LOG", self._audit, None),
        ]
        self.buttons: dict[str, QPushButton] = {}
        for row, text, slot, style in buttons:
            b = QPushButton(text)
            if style:
                b.setObjectName(style)
            b.clicked.connect(slot)
            row.addWidget(b)
            self.buttons[text] = b
        row1.addStretch(1)
        row2.addStretch(1)
        users.body.addLayout(row1)
        users.body.addLayout(row2)
        privacy = QLabel("Owners manage accounts, not private content: conversations and memories of other users "
                         "cannot be opened from here.")
        privacy.setWordWrap(True)
        privacy.setObjectName("HudLabel")
        users.body.addWidget(privacy)
        grid.addWidget(users, 0, 0, 2, 1)

        security = HudPanel("Account security & privacy")
        sform = QFormLayout()
        self.fields: dict[str, QWidget] = {}
        for key, label, kind in self.SECURITY_FIELDS:
            if kind is bool:
                widget = QCheckBox(label)
                sform.addRow(widget)
            else:
                widget = QSpinBox()
                widget.setRange(*kind)
                sform.addRow(label, widget)
            self.fields[key] = widget
        self.default_title = QLineEdit()
        self.default_title.setMaxLength(40)
        sform.addRow("Default owner title", self.default_title)
        save = QPushButton("SAVE SECURITY SETTINGS")
        save.setObjectName("Primary")
        save.clicked.connect(self._save_security)
        sform.addRow(save)
        security.body.addLayout(sform)
        grid.addWidget(security, 0, 1)

        db = HudPanel("Database")
        self.db_view = QPlainTextEdit()
        self.db_view.setReadOnly(True)
        self.db_view.setMinimumHeight(160)
        db_row = QHBoxLayout()
        for text, slot in (("REFRESH", lambda: self.refresh_db(False)), ("INTEGRITY CHECK", lambda: self.refresh_db(True)),
                           ("BACKUP NOW", self._backup)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            db_row.addWidget(b)
        db.body.addWidget(self.db_view, 1)
        db.body.addLayout(db_row)
        grid.addWidget(db, 1, 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    # -- helpers ---------------------------------------------------------------------
    def _note(self, text: str, ok: bool = True) -> None:
        self.status.setStyleSheet(f"color: {theme.GREEN if ok else theme.RED};")
        self.status.setText(text)

    def _error(self, text: str) -> None:
        self._note(text, ok=False)

    def clear(self) -> None:
        self.table.setRowCount(0)
        self._users = []
        self.db_view.clear()
        self.status.setText("")

    def _selected(self) -> dict | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self._error("Select an account first.")
            return None
        return self._users[rows[0].row()]

    def _sensitive(self, label: str, action_text: str, fn, done) -> None:
        run_sensitive(self, self._c, self.activity, label, action_text, fn, done, self._error)

    # -- loading -----------------------------------------------------------------------
    def refresh(self) -> None:
        session = self._c.session
        if session is None:
            return
        self._c.track(self.activity, "ENUMERATING ACCOUNTS", lambda: self._auth.list_users(session), self._show_users,
                      self._error, done_label="ACCOUNTS LOADED")
        policy = self._c.core.settings.section("accounts")
        for key, _label, kind in self.SECURITY_FIELDS:
            widget = self.fields[key]
            widget.setChecked(policy[key]) if kind is bool else widget.setValue(policy[key])
        self.default_title.setText(policy["owner_title_default"])
        self.refresh_db(False)

    def _show_users(self, users: list[dict]) -> None:
        self._users = users
        self.table.setRowCount(len(users))
        for r, u in enumerate(users):
            values = [u["username"], u["display_name"], u["role"].upper(), u["status"].upper(),
                      (u["last_login_at"] or "never").replace("T", " "), "YES" if u["locked"] else "",
                      "YES" if u["must_change_password"] else ""]
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                if u["status"] == "disabled":
                    item.setForeground(theme.qcolor(theme.MUTED))
                if c == 2 and u["role"] == "owner":
                    item.setForeground(theme.qcolor(theme.AMBER))
                self.table.setItem(r, c, item)

    def refresh_db(self, integrity: bool) -> None:
        session, core = self._c.session, self._c.core
        if session is None:
            return

        def show(status: dict) -> None:
            lines = []
            for key, value in status.items():
                if isinstance(value, dict):
                    lines.append(f"{key}:")
                    lines.extend(f"    {k:<20} {v}" for k, v in value.items())
                else:
                    lines.append(f"{key:<22} {value}")
            self.db_view.setPlainText("\n".join(lines))

        self._c.track(self.activity, "RUNNING INTEGRITY CHECK" if integrity else "READING DATABASE STATUS",
                      lambda: database_status(core.db, core.auth, session, integrity=integrity), show, self._error,
                      done_label="DATABASE OK")

    # -- account actions -------------------------------------------------------------------
    def _create(self) -> None:
        dialog = CreateUserDialog(self)
        if not dialog.exec():
            return
        session = self._c.session
        values = (dialog.username.text().strip(), dialog.display.text().strip(), dialog.password.text() or None)

        def done(result) -> None:
            _user_id, temporary = result
            self.refresh()
            self._note(f"Account '{values[0]}' created.")
            if temporary:
                show_secret_once(self, "Temporary password", f"Temporary password for '{values[0]}':", temporary)

        self._c.track(self.activity, "PROVISIONING ACCOUNT", lambda: self._auth.create_user(session, *values), done,
                      self._error, done_label="ACCOUNT CREATED")

    def _edit_name(self) -> None:
        user = self._selected()
        if not user:
            return
        name, ok = QInputDialog.getText(self, "Edit display name", "Display name:", text=user["display_name"])
        if not ok:
            return
        session = self._c.session
        self._c.track(self.activity, "UPDATING ACCOUNT", lambda: self._auth.update_user(session, user["id"], name),
                      lambda _r: (self.refresh(), self._note("Display name updated.")), self._error, done_label="SAVED")

    def _permissions(self) -> None:
        user = self._selected()
        if not user:
            return
        if user["role"] == "owner":
            self._error("The owner always has every permission.")
            return
        dialog = PermissionsDialog(self, user)
        if not dialog.exec():
            return
        session = self._c.session
        wanted = {p: box.isChecked() for p, box in dialog.boxes.items() if p not in OWNER_ONLY}

        def work() -> None:
            for permission, granted in wanted.items():
                if (permission in user["permissions"]) != granted:
                    self._auth.set_permission(session, user["id"], permission, granted)

        self._c.track(self.activity, "UPDATING PERMISSIONS", work,
                      lambda _r: (self.refresh(), self._note("Permissions saved.")), self._error, done_label="SAVED")

    def _reset(self) -> None:
        user = self._selected()
        if not user:
            return
        if QMessageBox.question(self, "Reset password", f"Reset the password of '{user['username']}'? They will be "
                                "signed out everywhere and must choose a new password.") != QMessageBox.StandardButton.Yes:
            return
        session = self._c.session
        self._sensitive("RESETTING CREDENTIALS", f"reset the password of '{user['username']}'",
                        lambda: self._auth.reset_password(session, user["id"]),
                        lambda temp: (self.refresh(), show_secret_once(self, "Temporary password",
                                                                         f"New temporary password for '{user['username']}':",
                                                                         temp)))

    def _toggle_status(self) -> None:
        user = self._selected()
        if not user:
            return
        new = "active" if user["status"] == "disabled" else "disabled"
        session = self._c.session
        self._sensitive("UPDATING ACCOUNT STATUS", f"{'reactivate' if new == 'active' else 'disable'} '{user['username']}'",
                        lambda: self._auth.set_status(session, user["id"], new),
                        lambda _r: (self.refresh(), self._note(f"Account '{user['username']}' is now {new}.")))

    def _transfer(self) -> None:
        user = self._selected()
        if not user:
            return
        if QMessageBox.question(self, "Transfer ownership",
                                f"Make '{user['username']}' the owner? You will become a normal user and lose access "
                                "to this page.") != QMessageBox.StandardButton.Yes:
            return
        session = self._c.session
        self._sensitive("TRANSFERRING OWNERSHIP", f"make '{user['username']}' the owner",
                        lambda: self._auth.transfer_ownership(session, user["id"]),
                        lambda _r: (self._note("Ownership transferred."), self._c.core.accounts.refresh_profile()))

    def _delete(self) -> None:
        user = self._selected()
        if not user:
            return
        if QMessageBox.question(self, "Delete account",
                                f"Permanently delete '{user['username']}' and ALL their conversations, memories, tasks "
                                "and events? This cannot be undone.") != QMessageBox.StandardButton.Yes:
            return
        session = self._c.session
        self._sensitive("DELETING ACCOUNT", f"delete '{user['username']}'",
                        lambda: self._auth.delete_user(session, user["id"]),
                        lambda _r: (self.refresh(), self._note(f"Account '{user['username']}' deleted.")))

    def _audit(self) -> None:
        session = self._c.session

        def show(entries: list[dict]) -> None:
            lines = [f"{e['timestamp'].replace('T', ' ')}  {e['action']:<20} by {e['actor'] or '-':<12} "
                     f"target {e['target'] or '-':<12} {json.loads(e['metadata'] or '{}')}" for e in entries]
            box = QMessageBox(self)
            box.setWindowTitle("Administrative audit log")
            box.setText("Account and security changes (newest first):")
            box.setDetailedText("\n".join(lines) or "No entries.")
            box.exec()

        self._c.track(self.activity, "READING AUDIT LOG", lambda: self._auth.audit_log(session, 300), show, self._error,
                      done_label="LOG LOADED")

    # -- settings & database ------------------------------------------------------------------
    def _save_security(self) -> None:
        changes = {key: (self.fields[key].isChecked() if kind is bool else self.fields[key].value())
                   for key, _label, kind in self.SECURITY_FIELDS}
        changes["owner_title_default"] = self.default_title.text().strip()
        session, settings = self._c.session, self._c.core.settings
        self._sensitive("APPLYING SECURITY POLICY", "change account security settings",
                        lambda: self._auth.update_security_settings(session, settings, changes),
                        lambda changed: self._note(f"Saved {len(changed)} setting(s)." if changed else "No changes."))

    def _backup(self) -> None:
        db = self._c.core.db
        session = self._c.session

        def work():
            self._auth.require(session, "users.manage")
            return db.backup(reason="manual")

        self._c.track(self.activity, "WRITING BACKUP", work,
                      lambda path: (self._note(f"Backup written: {path}"), self.refresh_db(False)), self._error,
                      done_label="BACKUP COMPLETE")
