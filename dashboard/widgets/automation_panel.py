"""AUTOMATION page (Module 4): current task, confirmations, emergency stop, history, apps & folders.

Every button calls the real services: commands typed here go through the same
conversation path as chat and voice; APPROVE/DENY and EMERGENCY STOP call the
automation controller; the APPS & FOLDERS and SETTINGS tabs go through
``AutomationAdmin`` (owner permission + password confirmation checked there).
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
                               QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
                               QVBoxLayout, QWidget)

from dashboard.dashboard_controller import friendly_message
from dashboard.styles import theme
from dashboard.widgets.activity import ScanIndicator
from dashboard.widgets.hud import HudPanel, hud_label
from dashboard.widgets.reauth import run_sensitive

STEP_ICONS = {"pending": ("○", theme.MUTED), "awaiting_confirmation": ("?", theme.AMBER), "running": ("◔", theme.CYAN),
              "completed": ("✓", theme.GREEN), "unverified": ("!", theme.AMBER), "failed": ("✗", theme.RED),
              "denied": ("⊘", theme.RED), "cancelled": ("–", theme.MUTED), "skipped": ("–", theme.MUTED)}
STATUS_COLORS = {"completed": theme.GREEN, "partially_completed": theme.AMBER, "failed": theme.RED,
                 "cancelled": theme.MUTED, "running": theme.CYAN, "awaiting_confirmation": theme.AMBER,
                 "pending": theme.MUTED}
RISK_COLORS = {"low": theme.GREEN, "medium": theme.AMBER, "high": theme.RED}
KINDS = ["editor", "browser", "office", "utility", "media", "communication", "files", "other"]


def _table(columns: list[str], stretch: int) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    header = table.horizontalHeader()
    for col in range(len(columns)):
        header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch if col == stretch
                                    else QHeaderView.ResizeMode.ResizeToContents)
    return table


class AutomationPanel(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self._c = controller
        self._core = controller.core
        self._task: dict | None = None
        self._history: list[dict] = []
        self._apps: list[dict] = []
        self._folders: list[dict] = []
        self._memories: list[dict] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        top = QHBoxLayout()
        self.activity = ScanIndicator("AUTOMATION STANDBY")
        top.addWidget(self.activity, 1)
        self.stop_button = QPushButton("⛔  EMERGENCY STOP")
        self.stop_button.setObjectName("Emergency")
        self.stop_button.setToolTip("Stop every running automation task and command now (also: say “stop everything”)")
        self.stop_button.clicked.connect(self.emergency_stop)
        top.addWidget(self.stop_button)
        layout.addLayout(top)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_task_tab(), "TASK")
        self.tabs.addTab(self._build_history_tab(), "HISTORY")
        self.tabs.addTab(self._build_apps_tab(), "APPS && FOLDERS")  # && shows one & in a Qt tab label
        self.tabs.addTab(self._build_memory_tab(), "MEMORY")
        self.tabs.addTab(self._build_settings_tab(), "SETTINGS")
        self.tabs.currentChanged.connect(lambda _i: self.refresh())
        layout.addWidget(self.tabs, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self.refresh)

    # -- building --------------------------------------------------------------------------------
    def _build_task_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        command = HudPanel("Command")
        row = QHBoxLayout()
        self.command = QLineEdit()
        self.command.setPlaceholderText("e.g. organise my Downloads · open VS Code and my project · check my disk space")
        self.command.returnPressed.connect(self._run_command)
        run = QPushButton("▶  RUN")
        run.setObjectName("Primary")
        run.clicked.connect(self._run_command)
        row.addWidget(self.command, 1)
        row.addWidget(run)
        command.body.addLayout(row)
        command.body.addWidget(hud_label("Same as typing it in the chat or saying it after “Hey DUMBOO”. Risky "
                                         "actions always ask first."))
        v.addWidget(command)

        self.confirm_panel = HudPanel("Confirmation required")
        head = QHBoxLayout()
        self.risk_tag = QLabel("MEDIUM RISK")
        self.risk_tag.setObjectName("Tag")
        self.confirm_question = QLabel("")
        self.confirm_question.setWordWrap(True)
        head.addWidget(self.risk_tag)
        head.addWidget(self.confirm_question, 1)
        self.confirm_panel.body.addLayout(head)
        self.confirm_details = QPlainTextEdit()
        self.confirm_details.setReadOnly(True)
        self.confirm_details.setMaximumHeight(160)
        self.confirm_panel.body.addWidget(self.confirm_details)
        buttons = QHBoxLayout()
        self.approve_button = QPushButton("✓  APPROVE")
        self.approve_button.setObjectName("Primary")
        self.approve_button.clicked.connect(lambda: self._decide(True))
        self.deny_button = QPushButton("✗  DENY")
        self.deny_button.setObjectName("Danger")
        self.deny_button.clicked.connect(lambda: self._decide(False))
        buttons.addStretch(1)
        buttons.addWidget(self.deny_button)
        buttons.addWidget(self.approve_button)
        self.confirm_panel.body.addLayout(buttons)
        self.confirm_panel.hide()
        v.addWidget(self.confirm_panel)

        current = HudPanel("Current task")
        title_row = QHBoxLayout()
        self.task_title = QLabel("No task yet.")
        self.task_title.setObjectName("HudValue")
        self.task_title.setWordWrap(True)
        self.task_status = QLabel("IDLE")
        self.task_status.setObjectName("TagMuted")
        self.cancel_button = QPushButton("CANCEL TASK")
        self.cancel_button.setObjectName("Danger")
        self.cancel_button.clicked.connect(self._cancel_task)
        self.cancel_button.setEnabled(False)
        title_row.addWidget(self.task_title, 1)
        title_row.addWidget(self.task_status)
        title_row.addWidget(self.cancel_button)
        current.body.addLayout(title_row)
        self.steps = QListWidget()
        current.body.addWidget(self.steps, 1)
        self.task_summary = QLabel("")
        self.task_summary.setWordWrap(True)
        self.task_summary.setObjectName("HudLabel")
        current.body.addWidget(self.task_summary)
        v.addWidget(current, 1)
        return page

    def _build_history_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        split = QSplitter()
        self.history_table = _table(["#", "TIME", "REQUEST", "STATUS", "STEPS", "VIA"], 2)
        self.history_table.itemSelectionChanged.connect(self._show_history_details)
        split.addWidget(self.history_table)
        self.history_details = QPlainTextEdit()
        self.history_details.setReadOnly(True)
        split.addWidget(self.history_details)
        split.setSizes([620, 420])
        v.addWidget(split, 1)
        row = QHBoxLayout()
        refresh = QPushButton("REFRESH")
        refresh.clicked.connect(self.refresh)
        save = QPushButton("SAVE AS WORKFLOW")
        save.clicked.connect(self._save_workflow)
        delete = QPushButton("DELETE")
        delete.setObjectName("Danger")
        delete.clicked.connect(self._delete_history)
        clear = QPushButton("CLEAR HISTORY")
        clear.setObjectName("Danger")
        clear.clicked.connect(self._clear_history)
        for b in (refresh, save, delete):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(clear)
        v.addLayout(row)
        return page

    def _build_apps_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        apps = HudPanel("Approved applications")
        self.apps_table = _table(["NAME", "KIND", "INSTALLED", "PROGRAM", "ALSO CALLED"], 3)
        apps.body.addWidget(self.apps_table, 1)
        row = QHBoxLayout()
        self.add_app_button = QPushButton("+ APPROVE PROGRAM…")
        self.add_app_button.clicked.connect(self._add_app)
        self.remove_app_button = QPushButton("REMOVE")
        self.remove_app_button.setObjectName("Danger")
        self.remove_app_button.clicked.connect(self._remove_app)
        row.addWidget(self.add_app_button)
        row.addWidget(self.remove_app_button)
        row.addStretch(1)
        apps.body.addLayout(row)
        v.addWidget(apps, 1)
        folders = HudPanel("Permitted folders (DUMBOO can only touch files inside these)")
        self.folders_table = _table(["FOLDER", "ACCESS", "EXISTS"], 0)
        folders.body.addWidget(self.folders_table, 1)
        row = QHBoxLayout()
        self.add_folder_button = QPushButton("+ ADD FOLDER…")
        self.add_folder_button.clicked.connect(self._add_folder)
        self.toggle_write_button = QPushButton("READ-ONLY / READ-WRITE")
        self.toggle_write_button.clicked.connect(self._toggle_write)
        self.remove_folder_button = QPushButton("REMOVE")
        self.remove_folder_button.setObjectName("Danger")
        self.remove_folder_button.clicked.connect(self._remove_folder)
        for b in (self.add_folder_button, self.toggle_write_button, self.remove_folder_button):
            row.addWidget(b)
        row.addStretch(1)
        folders.body.addLayout(row)
        self.admin_note = hud_label("Only the owner can change these (your password is asked once every 5 minutes).")
        folders.body.addWidget(self.admin_note)
        v.addWidget(folders, 1)
        return page

    def _build_memory_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        panel = HudPanel("What DUMBOO remembers for automation (yours only)")
        self.memory_table = _table(["SUBJECT", "VALUE", "CHECK", "SAVED"], 1)
        panel.body.addWidget(self.memory_table, 1)
        row = QHBoxLayout()
        edit = QPushButton("EDIT")
        edit.clicked.connect(self._edit_memory)
        delete = QPushButton("DELETE")
        delete.setObjectName("Danger")
        delete.clicked.connect(self._delete_memory)
        row.addWidget(edit)
        row.addWidget(delete)
        row.addStretch(1)
        panel.body.addLayout(row)
        panel.body.addWidget(hud_label("Say: “remember that my DUMBOO project is in this folder”, “remember my usual "
                                       "coding application is VS Code”, “remember this workflow as morning setup”."))
        v.addWidget(panel, 1)
        return page

    def _build_settings_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        panel = HudPanel("Alerts and safety")
        form = QFormLayout()
        self.alerts_enabled = QCheckBox("Show resource alerts")
        self.cpu_alert = self._spin(50, 100, "%")
        self.ram_alert = self._spin(50, 100, "%")
        self.disk_alert = self._spin(1, 50, "% free")
        self.battery_alert = self._spin(5, 50, "%")
        self.vision = QComboBox()
        self.vision.addItem("ask every time", "ask")
        self.vision.addItem("allow without asking", "allow")
        self.vision.addItem("never send screenshots", "never")
        self.confirm_timeout = self._spin(30, 3600, " s")
        form.addRow(self.alerts_enabled)
        form.addRow("CPU alert above", self.cpu_alert)
        form.addRow("Memory alert above", self.ram_alert)
        form.addRow("Disk alert below", self.disk_alert)
        form.addRow("Battery alert at", self.battery_alert)
        form.addRow("Screenshots to the AI", self.vision)
        form.addRow("Confirmation timeout", self.confirm_timeout)
        panel.body.addLayout(form)
        save = QPushButton("SAVE AUTOMATION SETTINGS")
        save.setObjectName("Primary")
        save.clicked.connect(self._save_settings)
        self.settings_save = save
        panel.body.addWidget(save)
        panel.body.addStretch(1)
        v.addWidget(panel, 1)
        return page

    @staticmethod
    def _spin(lo: int, hi: int, suffix: str) -> QSpinBox:
        box = QSpinBox()
        box.setRange(lo, hi)
        box.setSuffix(suffix)
        return box

    # -- helpers --------------------------------------------------------------------------------------
    def _note(self, text: str, ok: bool = True) -> None:
        self.status.setStyleSheet(f"color: {theme.GREEN if ok else theme.RED};")
        self.status.setText(text)

    def _error(self, text: str) -> None:
        self._note(text, ok=False)

    def _user_id(self) -> int | None:
        user = self._core.accounts.current_user()
        return user.id if user else None

    def apply_access(self, permissions) -> None:
        admin = "automation.admin" in set(permissions)
        for b in (self.add_app_button, self.remove_app_button, self.add_folder_button, self.toggle_write_button,
                  self.remove_folder_button, self.settings_save):
            b.setEnabled(admin)
        self.admin_note.setText("Only the owner can change these (your password is asked once every 5 minutes)."
                                if admin else "Ask the owner to approve programs or folders.")

    def clear(self) -> None:
        self._task = None
        self._history = []
        self.steps.clear()
        self.task_title.setText("No task yet.")
        self.task_status.setText("IDLE")
        self.task_summary.setText("")
        self.confirm_panel.hide()
        for table in (self.history_table, self.memory_table):
            table.setRowCount(0)
        self.history_details.clear()
        self.status.setText("")

    def schedule_refresh(self) -> None:
        if self.isVisible():
            self._refresh_timer.start(400)

    # -- live task -----------------------------------------------------------------------------------------
    def on_task(self, task: dict) -> None:
        self._task = task
        status = task["status"]
        self.task_title.setText(task["title"])
        self.task_status.setText(status.replace("_", " ").upper())
        self.task_status.setStyleSheet(f"background: {STATUS_COLORS.get(status, theme.LINE)}; color: {theme.BG};"
                                       " padding: 2px 8px;")
        running = status in ("running", "pending", "awaiting_confirmation")
        self.cancel_button.setEnabled(running)
        self.steps.clear()
        for n, step in enumerate(task["steps"], 1):
            icon, color = STEP_ICONS.get(step["status"], ("•", theme.MUTED))
            line = f"{icon}  {n}. {step['description'] or step['capability']}"
            if step.get("risk") and step["risk"] != "low":
                line += f"   [{step['risk'].upper()}]"
            detail = step.get("error") or step.get("verification") or ""
            if detail:
                line += f"\n      {'verified: ' if step['status'] == 'completed' else ''}{detail[:220]}"
            item = QListWidgetItem(line)
            item.setForeground(theme.qcolor(color))
            self.steps.addItem(item)
        self.task_summary.setText(task.get("error") or "")
        if running and not self.activity.active:
            self.activity.begin("EXECUTING TASK")
        elif not running and self.activity.active:
            self.activity.end(status == "completed", status.replace("_", " ").upper())
        awaiting = task.get("awaiting")
        if status == "awaiting_confirmation" and awaiting:
            self.risk_tag.setText(f"{awaiting['risk'].upper()} RISK")
            self.risk_tag.setStyleSheet(f"background: {RISK_COLORS.get(awaiting['risk'], theme.AMBER)}; "
                                        f"color: {theme.BG}; padding: 2px 8px;")
            self.confirm_question.setText(("Run this plan?" if awaiting.get("plan") else "Confirm: ")
                                          + ("" if awaiting.get("plan") else awaiting["description"]))
            self.confirm_details.setPlainText(awaiting.get("details") or "(no further details)")
            self.confirm_panel.show()
        else:
            self.confirm_panel.hide()
        if not running:
            self.schedule_refresh()

    def on_stopped(self, report: dict) -> None:
        self._note(f"Emergency stop: {report['tasks']} task(s) stopped. Completed steps are not undone.", ok=False)

    # -- actions ---------------------------------------------------------------------------------------------------
    def _run_command(self) -> None:
        text = self.command.text().strip()
        if text and self._c.send_text(text):
            self.command.clear()
            self._note("Sent. Results appear here and in the chat.")

    def _decide(self, approve: bool) -> None:
        task, session = self._task, self._c.session
        if task is None or session is None:
            return
        automation = self._core.automation
        self._c.run_async(lambda: automation.confirm(session, task["id"], approve),
                          lambda t: self._note("Approved." if approve else "Declined.") if t is not None else
                          self._error("That question is no longer waiting for an answer."), self._error)

    def _cancel_task(self) -> None:
        task, session = self._task, self._c.session
        if task is None or session is None:
            return
        automation = self._core.automation
        self._c.run_async(lambda: automation.cancel(session, task["id"]),
                          lambda _t: self._note("Cancel requested."), self._error)

    def emergency_stop(self) -> None:
        automation = self._core.automation
        self.activity.begin("EMERGENCY STOP")
        self._c.run_async(lambda: automation.emergency_stop("dashboard"),
                          lambda report: (self.activity.end(False, "STOPPED"), self.on_stopped(report)),
                          lambda message: (self.activity.end(False, "FAILED"), self._error(message)))

    # -- loading -----------------------------------------------------------------------------------------------------
    def refresh(self) -> None:
        session = self._c.session
        if session is None:
            return
        tab = self.tabs.currentIndex()
        core = self._core
        if tab == 1:
            self._c.track(self.activity, "LOADING TASK HISTORY", lambda: core.automation_history.list(session, 200),
                          self._show_history, self._error, done_label="HISTORY SYNCHRONISED")
        elif tab == 2:
            admin = core.automation_admin
            self._c.track(self.activity, "SCANNING APPLICATIONS",
                          lambda: (admin.applications(session), admin.folders(session)), self._show_apps, self._error,
                          done_label="REGISTRY VERIFIED")
        elif tab == 3:
            self._c.track(self.activity, "RETRIEVING MEMORY", lambda: core.automation_admin.memories(session),
                          self._show_memories, self._error, done_label="MEMORY SYNCHRONISED")
        elif tab == 4:
            self._c.run_async(lambda: core.automation_admin.settings(session), self._show_settings, self._error)

    def _show_history(self, rows: list[dict]) -> None:
        self._history = rows
        self.history_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            done = sum(1 for s in row["steps"] if s["status"] == "completed")
            values = [str(row["id"]), row["created_at"][:16].replace("T", " "), row["title"],
                      row["status"].replace("_", " "), f"{done}/{len(row['steps'])}", row["source"]]
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                if c == 3:
                    item.setForeground(theme.qcolor(STATUS_COLORS.get(row["status"], theme.TEXT)))
                self.history_table.setItem(r, c, item)
        self.history_details.clear()

    def _selected_history(self) -> dict | None:
        rows = self.history_table.selectionModel().selectedRows()
        return self._history[rows[0].row()] if rows else None

    def _show_history_details(self) -> None:
        row = self._selected_history()
        if row is None:
            return
        lines = [f"Task #{row['id']} — {row['status'].replace('_', ' ')}", f"Request: {row['request']}",
                 f"Created {row['created_at']}  ·  finished {row['finished_at'] or '—'}  ·  via {row['source']}"
                 + (f"  ·  workflow “{row['workflow_name']}”" if row.get("workflow_name") else ""), ""]
        for n, step in enumerate(row["steps"], 1):
            icon = STEP_ICONS.get(step["status"], ("•", ""))[0]
            lines.append(f"{icon} {n}. {step['description']}  [{step['status']}"
                         + (f", {step['risk']} risk" if step.get("risk") else "") + "]")
            if step.get("verification"):
                lines.append(f"     verified: {step['verification']}")
            if step.get("error"):
                lines.append(f"     error: {step['error']}")
            for path in step.get("changed", [])[:10]:
                lines.append(f"     changed: {path}")
        if row.get("error"):
            lines += ["", f"Problem: {row['error']}"]
        self.history_details.setPlainText("\n".join(lines))

    def _save_workflow(self) -> None:
        row = self._selected_history()
        if row is None:
            self._error("Select a task first.")
            return
        name, ok = QInputDialog.getText(self, "Save as workflow", "Workflow name (e.g. morning setup):")
        if not ok or not name.strip():
            return
        text = f"remember this workflow as {name.strip()}"
        session, core = self._c.session, self._core

        def work():
            from automation.models import Step
            task = core.automation.submit(session, text, [Step("memory.save_workflow", {"name": name.strip(),
                                                                                        "task_id": row["id"]})])
            return core.automation.wait(task, 10)

        self._c.track(self.activity, "WRITING WORKFLOW", work,
                      lambda t: self._note(t.steps[0].message) if t.status.value == "completed"
                      else self._error(t.steps[0].error or t.error), self._error, done_label="WORKFLOW SAVED")

    def _delete_history(self) -> None:
        row = self._selected_history()
        if row is None:
            self._error("Select a task first.")
            return
        session, history = self._c.session, self._core.automation_history
        self._c.track(self.activity, "DELETING RECORD", lambda: history.delete(session, row["id"]),
                      lambda _r: (self.refresh(), self._note(f"Task #{row['id']} deleted from your history.")),
                      self._error)

    def _clear_history(self) -> None:
        if QMessageBox.question(self, "Clear automation history",
                                "Delete your whole automation task history? (Files are not affected.)") \
                != QMessageBox.StandardButton.Yes:
            return
        session, history = self._c.session, self._core.automation_history
        self._c.track(self.activity, "CLEARING HISTORY", lambda: history.clear(session, confirmed=True),
                      lambda n: (self.refresh(), self._note(f"{n} task(s) deleted.")), self._error)

    def _show_apps(self, result) -> None:
        apps, folders = result
        self._apps, self._folders = apps, folders
        self.apps_table.setRowCount(len(apps))
        for r, app in enumerate(apps):
            values = [app["name"], app["kind"], "yes" if app["installed"] else "NOT FOUND", app["path"],
                      app["aliases"]]
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                if c == 2:
                    item.setForeground(theme.qcolor(theme.GREEN if app["installed"] else theme.RED))
                self.apps_table.setItem(r, c, item)
        self.folders_table.setRowCount(len(folders))
        for r, f in enumerate(folders):
            values = [f["resolved"], "read + write" if f["write"] else "read only", "yes" if f["exists"] else "NO"]
            for c, value in enumerate(values):
                self.folders_table.setItem(r, c, QTableWidgetItem(value))

    def _admin_action(self, label: str, action_text: str, fn, done_text: str) -> None:
        run_sensitive(self, self._c, self.activity, label, action_text, fn,
                      lambda _r: (self.refresh(), self._note(done_text)), self._error)

    def _add_app(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose the program (.exe)", "C:\\Program Files",
                                              "Programs (*.exe)")
        if not path:
            return
        name, ok = QInputDialog.getText(self, "Program name", "Name DUMBOO should use (e.g. Spotify):")
        if not ok or not name.strip():
            return
        kind, ok = QInputDialog.getItem(self, "Kind of program", "Kind:", KINDS, 0, False)
        if not ok:
            return
        session, admin = self._c.session, self._core.automation_admin
        self._admin_action("APPROVING PROGRAM", "approve a program", lambda: admin.add_application(
            session, name.strip(), path, kind), f"{name.strip()} approved.")

    def _remove_app(self) -> None:
        rows = self.apps_table.selectionModel().selectedRows()
        if not rows:
            self._error("Select an application first.")
            return
        app = self._apps[rows[0].row()]
        session, admin = self._c.session, self._core.automation_admin
        self._admin_action("REMOVING PROGRAM", "remove an approved program",
                           lambda: admin.remove_application(session, app["key"]), f"{app['name']} removed.")

    def _folder_entries(self) -> list[dict]:
        return [{"path": f["path"], "write": f["write"]} for f in self._folders]

    def _set_folders(self, entries: list[dict], done: str) -> None:
        session, admin = self._c.session, self._core.automation_admin
        self._admin_action("UPDATING PERMISSIONS", "change permitted folders", lambda: admin.set_folders(session,
                                                                                                        entries), done)

    def _add_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose a folder DUMBOO may use")
        if not path:
            return
        write = QMessageBox.question(self, "Access", "Allow DUMBOO to create, move and change files there?\n"
                                                     "(No = read only)") == QMessageBox.StandardButton.Yes
        self._set_folders(self._folder_entries() + [{"path": path.replace("/", "\\"), "write": write}],
                          f"Permitted: {path}")

    def _toggle_write(self) -> None:
        rows = self.folders_table.selectionModel().selectedRows()
        if not rows:
            self._error("Select a folder first.")
            return
        entries = self._folder_entries()
        entries[rows[0].row()]["write"] = not entries[rows[0].row()]["write"]
        self._set_folders(entries, "Access changed.")

    def _remove_folder(self) -> None:
        rows = self.folders_table.selectionModel().selectedRows()
        if not rows:
            self._error("Select a folder first.")
            return
        entries = self._folder_entries()
        removed = entries.pop(rows[0].row())
        self._set_folders(entries, f"Removed {removed['path']}.")

    def _show_memories(self, rows: list[dict]) -> None:
        self._memories = rows
        self.memory_table.setRowCount(len(rows))
        for r, m in enumerate(rows):
            values = [m["key"], m["value"], m["state"], m["updated_at"][:16].replace("T", " ")]
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                if c == 2 and value in ("MISSING", "NOT APPROVED"):
                    item.setForeground(theme.qcolor(theme.RED))
                self.memory_table.setItem(r, c, item)

    def _selected_memory(self) -> dict | None:
        rows = self.memory_table.selectionModel().selectedRows()
        return self._memories[rows[0].row()] if rows else None

    def _edit_memory(self) -> None:
        m = self._selected_memory()
        if m is None:
            self._error("Select a memory first.")
            return
        value, ok = QInputDialog.getText(self, "Edit", f"{m['key']}:", text=m["value"])
        if not ok or not value.strip():
            return
        session, memory = self._c.session, self._core.memory
        self._c.track(self.activity, "WRITING MEMORY", lambda: memory.update(session, m["id"], value=value.strip()),
                      lambda _r: (self.refresh(), self._note("Memory updated.")), self._error,
                      done_label="MEMORY WRITTEN")

    def _delete_memory(self) -> None:
        m = self._selected_memory()
        if m is None:
            self._error("Select a memory first.")
            return
        session, memory = self._c.session, self._core.memory
        self._c.track(self.activity, "ERASING MEMORY", lambda: memory.forget(session, m["id"]),
                      lambda _r: (self.refresh(), self._note(f"Forgot “{m['key']}”.")), self._error,
                      done_label="MEMORY ERASED")

    def _show_settings(self, settings: dict) -> None:
        mon, screen, limits = settings["monitor"], settings["screen"], settings["limits"]
        self.alerts_enabled.setChecked(bool(mon.get("alerts_enabled", True)))
        self.cpu_alert.setValue(int(mon.get("cpu_alert_percent", 90)))
        self.ram_alert.setValue(int(mon.get("ram_alert_percent", 90)))
        self.disk_alert.setValue(int(mon.get("disk_alert_free_percent", 10)))
        self.battery_alert.setValue(int(mon.get("battery_alert_percent", 15)))
        self.vision.setCurrentIndex(max(0, self.vision.findData(screen.get("ai_vision", "ask"))))
        self.confirm_timeout.setValue(int(limits.get("confirmation_timeout_s", 300)))

    def _save_settings(self) -> None:
        session, admin = self._c.session, self._core.automation_admin
        monitor = {"alerts_enabled": self.alerts_enabled.isChecked(), "cpu_alert_percent": self.cpu_alert.value(),
                   "ram_alert_percent": self.ram_alert.value(), "disk_alert_free_percent": self.disk_alert.value(),
                   "battery_alert_percent": self.battery_alert.value()}
        vision = self.vision.currentData()
        timeout = self.confirm_timeout.value()

        def work():
            admin.update_settings(session, "monitor", monitor)
            admin.update_settings(session, "screen", {"ai_vision": vision})
            admin.update_settings(session, "limits", {"confirmation_timeout_s": timeout})

        self._admin_action("SAVING SETTINGS", "change automation settings", work, "Automation settings saved.")
