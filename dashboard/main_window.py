"""Main dashboard window, system tray and application launch."""

from __future__ import annotations

import logging
import signal
import sys
from datetime import datetime

from PySide6.QtCore import QElapsedTimer, QEvent, QObject, QPointF, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow,
                               QMenu, QMessageBox, QPushButton, QSizePolicy, QStackedWidget, QSystemTrayIcon,
                               QVBoxLayout, QWidget)

from app.application import AssistantCore
from app.event_bus import Events
from services.schedule_service import reminder_banner
from dashboard.core_animation import CoreWidget
from dashboard.dashboard_controller import DashboardController
from dashboard.global_hotkey import HOTKEY_LABEL, GlobalHotkey
from dashboard.styles import theme
from dashboard.widgets.conversation_panel import ConversationPanel
from dashboard.widgets.admin_panel import AdminPanel
from dashboard.widgets.automation_panel import AutomationPanel
from dashboard.widgets.research_panel import ResearchPanel
from dashboard.widgets.diagnostics_panel import DiagnosticsPanel
from dashboard.widgets.history_panel import HistoryPanel
from dashboard.widgets.login_page import LoginPage
from dashboard.widgets.memory_panel import MemoryPanel
from dashboard.widgets.profile_panel import ProfilePanel
from dashboard.widgets.hud import HudPanel, LevelBars, StatusDot, hud_label, hud_value
from dashboard.widgets.schedule_panel import SchedulePanel
from dashboard.widgets.settings_panel import SettingsPanel
from dashboard.widgets.system_panel import SystemPanel
from dashboard.widgets.tasks_panel import TasksPanel

log = logging.getLogger(__name__)

INSTANCE_KEY = "DUMBOO-single-instance"
LEVEL_STATES = {"LISTENING", "WAKING", "INTERRUPTED"}
AGENDA_KINDS = {"task", "schedule", "agenda", "reminder"}  # local replies that may change TASKS / SCHEDULE


def make_icon(color: str = theme.CYAN) -> QIcon:
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(color), 4))
    p.drawEllipse(QPointF(32, 32), 26, 26)
    p.setPen(QPen(QColor(color), 2))
    p.drawEllipse(QPointF(32, 32), 16, 16)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(theme.ORANGE))
    p.drawEllipse(QPointF(32, 32), 8, 8)
    p.end()
    return QIcon(pix)


class StatusRow(QWidget):
    def __init__(self, label: str) -> None:
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.dot = StatusDot()
        self.value = hud_value("…")
        self.value.setWordWrap(True)
        name = hud_label(label)
        name.setFixedWidth(70)
        row.addWidget(self.dot)
        row.addWidget(name)
        row.addWidget(self.value, 1)

    def set(self, ok: bool | None, text: str) -> None:
        self.dot.set_color(theme.MUTED if ok is None else (theme.GREEN if ok else theme.RED))
        self.value.setText(text)


class AssistantPage(QWidget):
    def __init__(self, controller: DashboardController) -> None:
        super().__init__()
        self._c = controller
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 18)
        layout.setSpacing(14)

        # left column: status and agenda
        left = QVBoxLayout()
        status = HudPanel("Core telemetry")
        self.rows = {key: StatusRow(label) for key, label in (
            ("user", "USER"), ("state", "STATE"), ("mic", "MIC"), ("wake", "WAKE"), ("stt", "STT"), ("tts", "TTS"),
            ("brain", "AI"), ("memory", "MEMORY"), ("automation", "AUTO"), ("research", "WEB"))}
        for row in self.rows.values():
            status.body.addWidget(row)
        self.rows["brain"].set(None, "checking configuration…")
        self.rows["automation"].set(None, "standby")
        self.rows["research"].set(None, "standby")
        status.body.addWidget(hud_label("Input level"))
        self.levels = LevelBars()
        status.body.addWidget(self.levels)
        left.addWidget(status)
        agenda = HudPanel("Agenda")
        self.agenda = QLabel("…")
        self.agenda.setWordWrap(True)
        self.agenda.setObjectName("HudValue")
        self.agenda.setAlignment(Qt.AlignmentFlag.AlignTop)
        agenda.body.addWidget(self.agenda, 1)
        left.addWidget(agenda, 1)
        left_w = QWidget()
        left_w.setLayout(left)
        left_w.setFixedWidth(300)
        layout.addWidget(left_w)

        # centre: animated core, transcript, controls
        center = QVBoxLayout()
        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.hide()
        center.addWidget(self.banner)
        self.core = CoreWidget()
        center.addWidget(self.core, 1)
        self.transcript = QLabel("›  waiting for input")
        self.transcript.setObjectName("Transcript")
        self.transcript.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.transcript.setWordWrap(True)
        center.addWidget(self.transcript)
        controls = QHBoxLayout()
        controls.addStretch(1)
        self.talk_button = QPushButton("●  TALK")
        self.talk_button.setObjectName("Primary")
        self.talk_button.setToolTip("Start listening now (no wake phrase needed)")
        self.talk_button.clicked.connect(controller.talk)
        self.stop_button = QPushButton("■  STOP")
        self.stop_button.setToolTip("Stop the answer (generation and speech)")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(controller.stop_speaking)
        self.voice_button = QPushButton()
        self.voice_button.setCheckable(True)
        self.voice_button.setToolTip("Speak answers aloud (text conversation keeps working either way)")
        self.voice_button.toggled.connect(self._toggle_voice)
        self.sleep_button = QPushButton("☾  SLEEP")
        self.sleep_button.setToolTip(f"Hide the dashboard; DUMBOO keeps listening for the wake phrase. Wake with {HOTKEY_LABEL}.")
        self.sleep_button.clicked.connect(lambda: controller.sleep("button"))
        self.emergency_button = QPushButton("⛔  STOP ALL")
        self.emergency_button.setObjectName("Emergency")
        self.emergency_button.setToolTip("Emergency stop: cancel every running automation task and command")
        self.emergency_button.clicked.connect(
            lambda: controller.run_async(lambda: controller.core.automation.emergency_stop("dashboard")))
        for b in (self.talk_button, self.stop_button, self.voice_button, self.sleep_button, self.emergency_button):
            controls.addWidget(b)
        controls.addStretch(1)
        center.addLayout(controls)
        layout.addLayout(center, 1)

        # right: conversation
        self.conversation = ConversationPanel()
        self.conversation.setFixedWidth(400)
        self.conversation.send_requested.connect(self._send)
        self.conversation.retry_requested.connect(controller.retry)
        self.conversation.cleared.connect(controller.clear_conversation)
        self.sync_voice_button()
        layout.addWidget(self.conversation)

        self._banner_timer = QTimer(self)
        self._banner_timer.setSingleShot(True)
        self._banner_timer.timeout.connect(self.banner.hide)

    def sync_voice_button(self, prefs: dict | None = None) -> None:
        """Voice output is a per-user preference (ACCOUNT page / this button)."""
        on = (prefs or {}).get("voice_output", "on") == "on"
        self.voice_button.blockSignals(True)
        self.voice_button.setChecked(on)
        self.voice_button.setText("🔊  VOICE ON" if on else "🔇  VOICE OFF")
        self.voice_button.blockSignals(False)

    def _toggle_voice(self, on: bool) -> None:
        self.voice_button.setText("🔊  VOICE ON" if on else "🔇  VOICE OFF")
        session, profile, accounts = self._c.session, self._c.core.profile, self._c.core.accounts
        if session is None:
            return
        self._c.run_async(lambda: profile.set(session, "voice_output", "on" if on else "off"),
                          lambda _r: accounts.refresh_profile())

    def _send(self, text: str) -> None:
        if self._c.send_text(text):
            self.conversation.accept_submission()

    def show_banner(self, text: str, error: bool = False, seconds: int = 8) -> None:
        self.banner.setObjectName("ErrorBanner" if error else "Banner")
        self.banner.style().unpolish(self.banner)
        self.banner.style().polish(self.banner)
        self.banner.setText(text)
        self.banner.show()
        self._banner_timer.start(seconds * 1000)


class TopBar(QFrame):
    def __init__(self, pages: list[str]) -> None:
        super().__init__()
        self.setObjectName("TopBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 12, 0)
        layout.setSpacing(4)
        logo = QLabel()
        logo.setFixedSize(18, 18)
        logo.setStyleSheet(f"background: {theme.CYAN};")
        title = QLabel("DUMBOO")
        title.setObjectName("Title")
        title.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        sub = QLabel("AI CORE")
        sub.setObjectName("HudLabel")
        layout.addWidget(logo)
        layout.addSpacing(8)
        layout.addWidget(title)
        layout.addSpacing(6)
        layout.addWidget(sub)
        sub.hide()  # Module 4 added a page; the subtitle makes room for eleven navigation tabs
        layout.addSpacing(12)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for index, name in enumerate(pages):
            b = QPushButton(name)
            b.setObjectName("Nav")
            b.setCheckable(True)
            b.setChecked(index == 0)
            b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)  # names are never clipped
            self.group.addButton(b, index)
            layout.addWidget(b)
        layout.addStretch(1)
        self.mic = QLabel("MIC —")
        self.mic.setObjectName("TagMuted")
        self.state = QLabel("SLEEPING")
        self.state.setObjectName("Tag")
        self.clock = QLabel("")
        self.clock.setObjectName("HudValue")
        self.clock.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self.power = QPushButton("EXIT")
        self.power.setObjectName("Danger")
        self.power.setToolTip("Shut DUMBOO down completely (stops the wake-word listener)")
        self.user = QLabel("SIGNED OUT")
        self.user.setObjectName("TagMuted")
        self.user.setMinimumWidth(60)  # on a narrow window a long name is cut here, not the navigation tabs
        self.lock = QPushButton("LOCK")
        self.lock.setToolTip("Sign out (your conversations and memories stay saved, private to you)")
        layout.addWidget(self.user)
        layout.addWidget(self.lock)
        layout.addSpacing(8)
        layout.addWidget(self.mic)
        layout.addWidget(self.state)
        layout.addSpacing(12)
        layout.addWidget(self.clock)
        layout.addSpacing(12)
        layout.addWidget(self.power)
        self.setFixedHeight(50)


class MainWindow(QMainWindow):
    PAGES = ["ASSISTANT", "AUTOMATION", "RESEARCH", "TASKS", "SCHEDULE", "HISTORY", "MEMORY", "ACCOUNT", "ADMIN", "SYSTEM",
             "SETTINGS", "DIAGNOSTICS", "SECURITY"]
    # Which permission each page needs (checked again in the services; hiding is only a convenience).
    PAGE_PERMISSION = {"SECURITY": "security.view", "AUTOMATION": "automation.basic", "RESEARCH": "research.web", "TASKS": "tasks.manage", "SCHEDULE": "tasks.manage", "MEMORY": "memory.use",
                       "ADMIN": "users.manage", "SYSTEM": "system.view", "SETTINGS": "settings.system",
                       "DIAGNOSTICS": "system.view"}

    def __init__(self, controller: DashboardController) -> None:
        super().__init__()
        self._c = controller
        self._core = controller.core
        self._quitting = False
        self._told_about_tray = False
        self._state = self._core.state.state.value
        self.setWindowTitle("DUMBOO")
        self.setWindowIcon(make_icon())
        self._activity_clock = QElapsedTimer()
        self._activity_clock.start()
        self._last_activity_ms = -10_000
        QApplication.instance().installEventFilter(self)  # clicks/keys/scrolls anywhere in DUMBOO = activity
        self.resize(1500, 920)
        self.setMinimumSize(1460, 720)  # twelve navigation tabs (Modules 4-5) need the width

        root = QWidget()
        root.setObjectName("Root")
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.top = TopBar(self.PAGES)
        v.addWidget(self.top)
        self.stack = QStackedWidget()
        self.assistant = AssistantPage(controller)
        self.automation = AutomationPanel(controller)
        self.research = ResearchPanel(controller)
        self.tasks = TasksPanel(controller)
        self.schedule = SchedulePanel(controller)
        self.history = HistoryPanel(controller)
        self.memory = MemoryPanel(controller)
        self.profile = ProfilePanel(controller)
        self.admin = AdminPanel(controller)
        self.system = SystemPanel(controller)
        self.settings = SettingsPanel(controller)
        self.diagnostics = DiagnosticsPanel(controller)
        from dashboard.widgets.security_panel import SecurityPanel  # Module 7

        self.security = SecurityPanel(controller)
        self.pages = [self.assistant, self.automation, self.research, self.tasks, self.schedule, self.history, self.memory, self.profile, self.admin,
                      self.system, self.settings, self.diagnostics, self.security]
        for page in self.pages:
            self.stack.addWidget(page)
        v.addWidget(self.stack, 1)

        # Sign-in screen in front of everything until a user is authenticated.
        self.login = LoginPage(controller)
        self.root_stack = QStackedWidget()
        self.root_stack.setObjectName("Root")  # dark background behind the sign-in screen
        self.root_stack.addWidget(self.login)   # 0: locked
        shell = QWidget()
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.addWidget(root)
        self.root_stack.addWidget(shell)        # 1: signed in
        self.setCentralWidget(self.root_stack)
        self._user: dict | None = None
        self._user_id: int | None = None

        self.top.group.idClicked.connect(self._switch_page)
        self.top.power.clicked.connect(self.confirm_quit)
        self.top.lock.clicked.connect(controller.logout)
        self.login.signed_in.connect(self._enter_shell)
        self.history.continue_requested.connect(self._continue_conversation)
        self.settings.saved.connect(lambda _c: self._apply_dashboard_settings())
        self.assistant.core.talk_requested.connect(controller.talk)          # click the blob = TALK
        self.assistant.core.blob_settings_changed.connect(self._save_blob_settings)

        self.tray = self._make_tray()
        controller.bridge.event.connect(self.on_event)

        self._clock = QTimer(self)
        self._clock.timeout.connect(self._tick)
        self._clock.start(1000)
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._core.publish_wake_status)
        self._status_timer.start(2000)
        self._agenda_timer = QTimer(self)
        self._agenda_timer.timeout.connect(self.refresh_agenda)
        self._agenda_timer.start(60_000)
        self._apply_dashboard_settings()
        self._tick()
        self._render_state(self._state, "", apply_visibility=False)  # launch() decides initial visibility
        if self._core.accounts.signed_in:     # remembered session restored before the window existed
            self._on_signed_in(self._core.accounts)
        else:
            self._lock_ui("")

    # -- tray -------------------------------------------------------------------------------
    def _make_tray(self) -> QSystemTrayIcon | None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.warning("System tray not available")
            return None
        tray = QSystemTrayIcon(make_icon(), self)
        tray.setToolTip("DUMBOO")
        menu = QMenu()
        actions = [
            ("Show dashboard", lambda: self._c.wake("tray")),
            ("Talk now", self._c.talk),
            ("Sleep", lambda: self._c.sleep("tray")),
        ]
        for text, slot in actions:
            act = QAction(text, menu)
            act.triggered.connect(slot)
            menu.addAction(act)
        self.mute_action = QAction("Mute microphone", menu)
        self.mute_action.setCheckable(True)
        self.mute_action.toggled.connect(self._c.set_mic_muted)
        menu.addAction(self.mute_action)
        menu.addSeparator()
        quit_action = QAction("Exit DUMBOO", menu)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(self._tray_activated)
        tray.show()
        self._tray_menu = menu
        return tray

    def _tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.isVisible():
                self._c.sleep("tray")
            else:
                self._c.wake("tray")

    def notify(self, title: str, message: str) -> None:
        if self.tray is not None and self.tray.supportsMessages():
            self.tray.showMessage(title, message, make_icon(), 8000)

    # -- window visibility ------------------------------------------------------------------------
    def show_dashboard(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def hide_dashboard(self) -> None:
        if self.tray is None:
            self.showMinimized()  # no tray: keep a taskbar entry so the user can come back
            return
        self.hide()
        if not self._told_about_tray:
            self._told_about_tray = True
            phrase = self._core.settings.get("wake_word", "phrase")
            self.notify("DUMBOO is sleeping", f"Say “{phrase.title()}”, press {HOTKEY_LABEL}, or use the tray icon.")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._quitting:
            event.accept()
            return
        event.ignore()  # the X button puts DUMBOO to sleep; Exit is explicit
        self._c.sleep("window")

    def confirm_quit(self) -> None:
        answer = QMessageBox.question(self, "Exit DUMBOO",
                                      "Shut DUMBOO down? The wake-word listener will stop until you start it again.")
        if answer == QMessageBox.StandardButton.Yes:
            self.quit_app()

    def quit_app(self) -> None:
        self._quitting = True
        QApplication.instance().quit()

    # -- periodic -----------------------------------------------------------------------------------
    def _tick(self) -> None:
        self.top.clock.setText(datetime.now().strftime("%H:%M  %a %d"))

    def _switch_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        page = self.stack.currentWidget()
        if hasattr(page, "refresh"):
            page.refresh()

    def _agenda_changed(self) -> None:
        self.refresh_agenda()
        if self.stack.currentWidget() in (self.tasks, self.schedule):
            self.stack.currentWidget().refresh()

    def refresh_agenda(self) -> None:
        core, session = self._core, self._c.session
        if session is None:
            self.assistant.agenda.setText("")
            return

        def load():
            if not core.auth.require(session).can("tasks.manage"):
                return [], [], []
            return core.schedule.today(session), core.schedule.upcoming(session, 7), core.tasks.list(session, False)[:5]

        def show(result) -> None:
            today, upcoming, tasks = result
            now = datetime.now()
            lines = ["TODAY"]
            todays = [e for e in today if e.starts_at >= now]
            lines += [f"  {e.starts_at:%H:%M}  {e.title}" for e in todays[:4]] or ["  nothing else scheduled"]
            if upcoming:
                lines += ["", "NEXT"] + [f"  {e.starts_at:%a %H:%M}  {e.title}" for e in upcoming[:3]]
            lines += ["", "TASKS"] + ([f"  [{t.priority[0].upper()}] {t.title}" + ("  ⚠" if t.is_overdue() else "")
                                       for t in tasks] or ["  no pending tasks"])
            self.assistant.agenda.setText("\n".join(lines))

        self._c.run_async(load, show)

    # -- accounts -------------------------------------------------------------------------------
    def _on_signed_in(self, payload) -> None:
        """A user was authenticated (sign-in, owner setup or remembered session)."""
        if not isinstance(payload, dict):  # restored before the window existed
            user = self._core.accounts.current_user()
            if user is None:
                self._lock_ui("")
                return
            prefs = self._core.profile.get(self._c.session)
            payload = {"username": user.username, "display_name": user.display_name, "role": user.role,
                       "title": prefs["title"], "must_change_password": user.must_change_password,
                       "permissions": sorted(user.permissions)}
        self._user = payload
        current = self._core.accounts.current_user()
        self._user_id = current.id if current else None
        self._apply_access(payload["permissions"])
        self.automation.apply_access(payload["permissions"])
        self.research.apply_access(payload["permissions"])
        self.top.user.setText(f"{payload['display_name'].upper()} · {payload['role'].upper()}")
        self.top.user.setToolTip(self.top.user.text())
        self.assistant.rows["user"].set(True, f"{payload['display_name']} ({payload['username']}) · "
                                              f"addressed as “{payload['title']}”")
        if payload.get("must_change_password"):
            self.root_stack.setCurrentIndex(0)
            self.login.require_password_change()
            return
        if self.root_stack.currentIndex() == 0 and self.login.scanner.mode in ("scanning", "granted"):
            return  # the login page finishes its ACCESS GRANTED animation, then emits signed_in
        self._enter_shell()

    def _enter_shell(self) -> None:
        if not self._core.accounts.signed_in:
            return
        user = self._user or {}
        self.root_stack.setCurrentIndex(1)
        self.stack.setCurrentIndex(0)
        self.top.group.button(0).setChecked(True)
        title = user.get("title", "")
        self.assistant.core.flash("IDENTITY CONFIRMED", theme.GREEN)
        self.assistant.show_banner(f"Welcome back{', ' + title if title else ''}. Your conversations and memories "
                                   "are loaded.", seconds=6)
        self.assistant.sync_voice_button(self._core.profile.get(self._c.session))
        self.refresh_agenda()
        self._refresh_memory_row()

    def _lock_ui(self, reason: str) -> None:
        """Signed out: hide every private view and clear what was shown."""
        self._user = None
        self._user_id = None
        self.automation.clear()
        self.research.clear()
        self.assistant.rows["research"].set(None, "standby")
        self.assistant.core.automation_task(None)
        self.assistant.rows["automation"].set(None, "standby")
        self.top.user.setText("SIGNED OUT")
        self.assistant.conversation.clear(emit=False)
        self.assistant.agenda.setText("")
        self.assistant.rows["user"].set(False, "nobody signed in")
        self.assistant.rows["memory"].set(None, "—")
        for page in (self.history, self.memory, self.profile, self.admin):
            page.clear()
        self.tasks.clear_view()
        self.schedule.clear_view()
        self.root_stack.setCurrentIndex(0)
        self.login.refresh(reason)

    def _apply_access(self, permissions) -> None:
        allowed = set(permissions)
        for index, name in enumerate(self.PAGES):
            needed = self.PAGE_PERMISSION.get(name)
            self.top.group.button(index).setVisible(needed is None or needed in allowed)
        current = self.PAGES[self.stack.currentIndex()]
        if self.PAGE_PERMISSION.get(current) and self.PAGE_PERMISSION[current] not in allowed:
            self.stack.setCurrentIndex(0)
            self.top.group.button(0).setChecked(True)

    def _refresh_memory_row(self) -> None:
        session, memory = self._c.session, self._core.memory
        if session is None:
            return
        self._c.run_async(lambda: memory.count(session),
                          lambda n: self.assistant.rows["memory"].set(True, f"{n} saved fact(s) · database online"),
                          lambda message: self.assistant.rows["memory"].set(False, message))

    def _continue_conversation(self, conversation: dict) -> None:
        """Re-open an earlier conversation in the chat; DUMBOO gets its recent context back."""
        try:
            self._c.continue_conversation(conversation)
        except Exception as exc:
            from dashboard.dashboard_controller import friendly_message
            self.history._error(friendly_message(exc))
            return
        chat = self.assistant.conversation
        chat.clear(emit=False)
        for m in conversation["messages"]:
            if m["role"] == "user":
                chat.add_message("voice" if m["input_mode"] == "voice" else "user", m["content"])
            elif m["status"] == "completed":
                chat.add_message("assistant", m["content"], markdown=True)
        chat.add_message("system", f"Continuing “{conversation['title']}”. New messages are added to it.")
        self.stack.setCurrentIndex(0)
        self.top.group.button(0).setChecked(True)

    _ACTIVITY_EVENTS = (QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress, QEvent.Type.Wheel)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Report dashboard use (at most every 2 s), so DUMBOO doesn't fall asleep while you work in its pages."""
        if event.type() in self._ACTIVITY_EVENTS and self.isVisible():
            now = self._activity_clock.elapsed()
            if now - self._last_activity_ms > 2000:
                self._last_activity_ms = now
                self._core.bus.publish(Events.UI_ACTIVITY, {})
        return False

    def _apply_dashboard_settings(self) -> None:
        dash = self._core.settings.section("dashboard")
        self.assistant.core.set_intensity(dash["animation_intensity"])
        self.assistant.core.set_blob(dash["blob_enabled"], dash["blob_sensitivity"])

    def _save_blob_settings(self, enabled: bool, sensitivity: float) -> None:
        """Right-click choice on the core: already applied; save it (owner settings) in the background."""
        changes = {"dashboard": {**self._core.settings.section("dashboard"), "blob_enabled": enabled,
                                 "blob_sensitivity": round(sensitivity, 2)}}
        self._c.run_async(lambda: self._c.update_system_settings(changes), lambda _changed: self.settings.load(),
                          lambda message: self.assistant.show_banner(
                              f"⚠  Blob setting applied for now but not saved: {message}", seconds=6))

    # -- events from the core -------------------------------------------------------------------------
    def on_event(self, name: str, payload: dict) -> None:
        a = self.assistant
        if name == Events.STATE_CHANGED:
            self._state = payload["new"]
            self._render_state(payload["new"], payload.get("reason", ""))
        elif name == Events.AUDIO_LEVEL:
            if not self.isVisible():
                return
            speech = payload["speech_prob"] >= self._core.settings.get("vad", "threshold")
            a.levels.add(min(1.0, payload["rms"] * 8), speech)
            if self._state in LEVEL_STATES:
                a.core.set_level(payload["rms"])
        elif name == Events.PLAYBACK_LEVEL:
            if self._state == "SPEAKING":
                a.core.set_level(payload["rms"], "bot")
        elif name == Events.SPEECH_STARTED:
            a.transcript.setText("›  hearing you…")
        elif name == Events.USER_TEXT:
            if payload.get("source") == "voice":
                a.transcript.setText(f"›  “{payload['text']}”")
        elif name == Events.AI_STARTED:
            voice = payload.get("source") == "voice"
            a.conversation.add_message("voice" if voice else "user", payload["text"])
            a.conversation.start_answer(payload["seq"], f"{payload['provider']} · {payload['model']}")
            a.core.on_message_sent()
        elif name == Events.RESPONSE_DELTA:
            a.conversation.update_answer(payload["seq"], payload["text"])
            a.core.on_answer_piece(len(payload.get("delta", "")))
        elif name == Events.AI_RETRYING:
            a.transcript.setText(f"›  {payload['reason']} Retrying in {payload['wait_s']} s…")
        elif name == Events.GREETING:
            a.conversation.add_message("assistant", payload["text"], markdown=True)
        elif name == Events.AI_SWITCHED:
            a.transcript.setText(f"›  {payload['from']} {payload['reason']}, answering with {payload['to']}…")
        elif name == Events.RESPONSE_CANCELLED:
            a.conversation.stop_answer(payload.get("seq"), "stopped")
        elif name == Events.AI_STATUS:
            ok = payload["ready"]
            a.rows["brain"].set(ok, payload["detail"])
            a.conversation.set_provider_tag(f"{payload['provider'].upper()} · {payload['model']}" if ok
                                            else "AI NOT CONFIGURED", ok)
            a.core.set_ai_info(payload["provider"], payload["model"], payload.get("turns", 0))
            self._ai_text = (payload["provider"].upper(), payload["model"]) if ok else ("NOT SET", "—")
            self._update_tags()
        elif name == Events.TRANSCRIPT_FAILED:
            a.transcript.setText(f"›  {payload.get('error', 'not understood')}")
        elif name == Events.UTTERANCE_REJECTED:
            a.transcript.setText(f"›  ignored a short sound ({payload.get('reason', '')})")
        elif name == Events.RESPONSE_READY:
            if self.research.waiting and payload.get("model") != "research":
                self.research.not_research()
            footer = f"{payload.get('provider', '')} · {payload.get('model', '')} · {payload.get('elapsed', 0):.1f} s"
            if payload.get("truncated"):
                footer += "  ·  answer cut at the output limit — ask “continue” or raise Max output tokens"
            a.conversation.finish_answer(payload["seq"], payload["text"], footer)
            a.core.on_answer_done()
            if payload.get("local") and payload.get("model") in AGENDA_KINDS:
                self._agenda_changed()   # a task / event / reminder was added or changed by voice or text
        elif name == Events.RESPONSE_FAILED:
            a.conversation.fail_answer(payload["seq"], payload.get("error", "The AI request failed."))
            a.core.on_answer_done()
        elif name == Events.REMINDER_DUE:
            text = reminder_banner(payload)
            a.show_banner("⏰  " + text, seconds=30)
            a.conversation.add_message("system", text)
            self.notify("DUMBOO reminder", text)
            self._agenda_changed()
        elif name == Events.ERROR:
            if payload.get("source") != "assistant":  # AI errors are shown in the conversation with RETRY
                a.show_banner(f"⚠  {payload.get('message', 'error')}", error=True)
        elif name == Events.MIC_STATUS:
            muted = self._core.mic.muted
            ok = payload.get("open")
            text = "muted" if muted else ("open · " + payload.get("device", "") if ok else (payload.get("error") or "closed"))
            a.rows["mic"].set(ok and not muted, text)
            self.top.mic.setText("MIC MUTED" if muted else ("MIC ON" if ok else "MIC OFF"))
            self._update_tags()
        elif name == Events.WAKE_SERVICE_STATUS:
            running = payload["running"] and not self._core.mic.muted
            detail = payload.get("detail", "")
            a.rows["wake"].set(running, ("listening · " if running else "NOT listening · ") + detail)
            self._wake_text = "ARMED" if running else "OFFLINE"
            self._update_tags()
        elif name == Events.STT_STATUS:
            a.rows["stt"].set(payload["ready"], payload["detail"])
        elif name == Events.TTS_STATUS:
            a.rows["tts"].set(payload["ready"], payload["detail"])
        elif name == Events.SETTINGS_CHANGED:
            self._apply_dashboard_settings()
        elif name == Events.USER_SIGNED_IN:
            self._on_signed_in(payload)
        elif name == Events.USER_SIGNED_OUT:
            self._lock_ui(payload.get("reason", "") if payload.get("reason") != "signed out" else "Signed out.")
        elif name == Events.PROFILE_CHANGED:
            a.sync_voice_button(payload)
            if self._user is not None:
                self._user["title"] = payload.get("title", self._user.get("title"))
                user = self._core.accounts.current_user()
                if user is not None:
                    self._user.update(display_name=user.display_name, role=user.role,
                                      permissions=sorted(user.permissions))
                    self._on_signed_in(dict(self._user))
        elif name == Events.MEMORY_RETRIEVAL:
            if payload.get("phase") == "start":
                a.core.memory_scan(True)
            else:
                a.core.memory_scan(False, payload.get("memories", 0), payload.get("messages", 0))
                a.rows["memory"].set(True, f"retrieved {payload.get('memories', 0)} fact(s), "
                                           f"{payload.get('messages', 0)} past message(s)")
        elif name == Events.MEMORY_CHANGED:
            action = payload.get("action")
            label, color = {"delete": ("MEMORY ERASED", theme.AMBER), "clear": ("MEMORY CORE CLEARED", theme.AMBER),
                            "unchanged": ("MEMORY VERIFIED", theme.CYAN)}.get(action, ("MEMORY WRITTEN", theme.VIOLET))
            a.core.flash(label, color)
            self.memory.schedule_refresh()
            self._refresh_memory_row()
        elif name == Events.HISTORY_CHANGED:
            self.history.schedule_refresh()
        elif name in (Events.AUTOMATION_TASK, Events.AUTOMATION_CONFIRM, Events.AUTOMATION_ANNOUNCE,
                      Events.AUTOMATION_STOPPED, Events.SYSTEM_ALERT):
            self._on_automation_event(name, payload)
        elif name in (Events.RESEARCH_PROGRESS, Events.RESEARCH_DONE):
            self._on_research_event(name, payload)

    def _on_research_event(self, name: str, payload: dict) -> None:
        """Module 5: real research stages drive the RESEARCH page, the core ring and the telemetry row."""
        if payload.get("user_id") != self._user_id:
            return
        a = self.assistant
        if name == Events.RESEARCH_PROGRESS:
            task = payload["task"]
            self.research.on_progress(task)
            status = {"partial": "partially_completed"}.get(task["status"], task["status"])
            a.core.automation_task({**task, "status": status})
            done = sum(1 for s in task["steps"] if s["status"] == "completed")
            a.rows["research"].set(None if task["status"] == "running" else task["status"] in ("completed", "partial"),
                                   f"{task['status']} · {done}/{len(task['steps'])} · {task['detail'][:40]}")
        else:
            self.research.on_done(payload)
            if payload.get("status") == "completed":
                a.core.flash("RESEARCH VERIFIED", theme.GREEN)
            elif payload.get("status") == "failed":
                a.core.flash("SEARCH FAILED", theme.RED)

    def _on_automation_event(self, name: str, payload: dict) -> None:
        """Module 4: task progress, confirmations, announcements, emergency stop, resource alerts."""
        a = self.assistant
        mine = payload.get("user_id") is None or payload.get("user_id") == self._user_id
        if name == Events.AUTOMATION_TASK and mine:
            task = payload["task"]
            self.automation.on_task(task)
            a.core.automation_task(task)
            done = sum(1 for s in task["steps"] if s["status"] == "completed")
            status = task["status"]
            ok = None if status in ("running", "pending", "awaiting_confirmation") else status == "completed"
            a.rows["automation"].set(ok, f"{status.replace('_', ' ')} · {done}/{len(task['steps'])} · "
                                         f"{task['title'][:40]}")
        elif name == Events.AUTOMATION_CONFIRM and mine:
            risk = payload.get("risk", "medium").upper()
            a.core.flash(f"CONFIRM · {risk} RISK", theme.AMBER, 2500)
            a.show_banner(f"⚠  {payload.get('question', 'Confirmation needed')} Say “yes” or “no”, or use "
                          "APPROVE / DENY on the AUTOMATION page.", seconds=20)
            if not self.isVisible():
                self.notify("DUMBOO needs your confirmation", payload.get("question", ""))
        elif name == Events.AUTOMATION_ANNOUNCE and mine:
            a.conversation.add_message("system", payload["text"])
            a.show_banner("✓  An automation task finished. Details are in the chat.", seconds=8)
            if not self.isVisible():
                self.notify("DUMBOO task finished", payload["text"][:200])
        elif name == Events.AUTOMATION_STOPPED:
            a.core.flash("EMERGENCY STOP", theme.RED, 3000)
            if payload.get("tasks"):
                a.show_banner(f"⛔  Emergency stop: {payload['tasks']} task(s) stopped. Completed steps are not "
                              "undone.", error=True, seconds=12)
            self.automation.on_stopped(payload)
        elif name == Events.SYSTEM_ALERT:
            a.show_banner(f"⚠  {payload['message']}", error=True, seconds=15)
            self.system.flag_alert(payload)
            if not self.isVisible():
                self.notify("DUMBOO resource alert", payload["message"])

    def _update_tags(self) -> None:
        core = self._core
        tags = [
            ("STATE", self._state),
            ("MIC", "MUTED" if core.mic.muted else ("OPEN" if core.pipeline.mic_ok else "OFFLINE")),
            ("WAKE", getattr(self, "_wake_text", "…")),
            ("AI", getattr(self, "_ai_text", ("…", "…"))[0]),
            ("MODEL", getattr(self, "_ai_text", ("…", "…"))[1]),
        ]
        self.assistant.core.set_tags(tags)

    def _render_state(self, state: str, reason: str, apply_visibility: bool = True) -> None:
        a = self.assistant
        a.core.set_state(state, reason)
        self.top.state.setText(state.replace("_", " "))
        short = reason if len(reason) <= 60 else reason[:57] + "…"
        a.rows["state"].set(state != "ERROR", f"{state.lower()}" + (f" · {short}" if short else ""))
        a.stop_button.setEnabled(state in ("SPEAKING", "PROCESSING", "STREAMING"))
        a.talk_button.setEnabled(state not in ("LISTENING", "WAKING") and not self._core.settings.get("dashboard", "text_only"))
        if state == "LISTENING":
            a.transcript.setText("›  listening…")
        if self.tray is not None:
            self.tray.setToolTip(f"DUMBOO — {state.lower()}")
        self._update_tags()
        if not apply_visibility:
            return
        if state == "SLEEPING":
            self.hide_dashboard()
        elif state == "WAKING":
            self.show_dashboard()
            self.stack.setCurrentIndex(0)
            self.top.group.button(0).setChecked(True)


def _claim_single_instance() -> QLocalServer | None:
    """Return a server if we are the first DUMBOO; otherwise wake the running one."""
    socket = QLocalSocket()
    socket.connectToServer(INSTANCE_KEY)
    if socket.waitForConnected(300):
        socket.write(b"wake")
        socket.flush()
        socket.waitForBytesWritten(300)
        socket.disconnectFromServer()
        return None
    QLocalServer.removeServer(INSTANCE_KEY)  # clean up after a crash
    server = QLocalServer()
    server.listen(INSTANCE_KEY)
    return server


def launch(core: AssistantCore, show: bool = False) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("DUMBOO")
    app.setQuitOnLastWindowClosed(False)
    server = _claim_single_instance()
    if server is None:
        print("DUMBOO is already running - asked it to show its dashboard.")
        return 0
    app.setStyleSheet(theme.STYLE_SHEET)

    controller = DashboardController(core)
    window = MainWindow(controller)
    server.newConnection.connect(lambda: (server.nextPendingConnection(), controller.wake("second-launch")))

    hotkey = GlobalHotkey(lambda: controller.wake("hotkey"))
    core.start()
    if hotkey.start():
        log.info("Global wake shortcut %s registered", HOTKEY_LABEL)
    else:
        log.info("Global wake shortcut unavailable: %s", hotkey.error)

    sleeping = core.state.state.value == "SLEEPING"
    if show or not sleeping or window.tray is None:
        if sleeping:
            controller.wake("startup")
        window.show_dashboard()
    else:
        phrase = core.settings.get("wake_word", "phrase").title()
        window.notify("DUMBOO started", f"Listening for “{phrase}”. Press {HOTKEY_LABEL} or use the tray icon to open.")
        window._told_about_tray = True

    def shutdown() -> None:
        hotkey.stop()
        if window.tray is not None:
            window.tray.hide()
        controller.shutdown()
        server.close()

    app.aboutToQuit.connect(shutdown)
    # Let Ctrl+C in the terminal close DUMBOO cleanly.
    signal.signal(signal.SIGINT, lambda *_: window.quit_app())
    keepalive = QTimer()
    keepalive.timeout.connect(lambda: None)  # lets Python handle signals during the Qt loop
    keepalive.start(300)
    return app.exec()
