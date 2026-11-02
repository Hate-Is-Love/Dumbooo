"""Password confirmation for sensitive owner actions.

The service layer decides when re-authentication is needed (it raises
``ReauthRequired``); the dashboard only asks for the password, verifies it
through ``AuthService.reauthenticate`` and then retries the action once.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget

from dashboard.dashboard_controller import friendly_message
from dashboard.widgets.login_page import password_field
from services.auth_service import ReauthRequired


class ReauthDialog(QDialog):
    def __init__(self, parent: QWidget, action: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("Confirm it's you")
        layout = QVBoxLayout(self)
        text = QLabel(f"Enter your password to {action}.")
        text.setWordWrap(True)
        self.password = password_field("your password")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(text)
        layout.addWidget(self.password)
        layout.addWidget(buttons)


def run_sensitive(parent: QWidget, controller, indicator, label: str, action_text: str, fn: Callable[[], Any],
                  on_done: Callable[[Any], None], on_error: Callable[[str], None], _retried: bool = False,
                  ask_password: Callable[[QWidget, str], str | None] | None = None) -> None:
    """Run ``fn``; if the service requires re-authentication, ask once for the password and retry."""

    def ask(widget: QWidget, text: str) -> str | None:
        dialog = ReauthDialog(widget, text)
        if dialog.exec():
            return dialog.password.text()
        return None

    asker = ask_password or ask

    def handle(exc: BaseException) -> None:
        if isinstance(exc, ReauthRequired) and not _retried:
            password = asker(parent, action_text)
            if not password:
                on_error("Cancelled: password not confirmed.")
                return
            controller.track(indicator, "VERIFYING IDENTITY",
                             lambda: controller.core.auth.reauthenticate(controller.session, password),
                             lambda _r: run_sensitive(parent, controller, indicator, label, action_text, fn, on_done,
                                                      on_error, _retried=True, ask_password=asker),
                             on_error, done_label="IDENTITY CONFIRMED")
            return
        on_error(friendly_message(exc))

    controller.track(indicator, label, fn, on_done, on_exception=handle)
