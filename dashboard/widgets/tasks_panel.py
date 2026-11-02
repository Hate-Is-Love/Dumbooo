"""Tasks page: create, edit, complete and delete tasks (also by voice: "add a task to … by Friday")."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from PySide6.QtCore import QDate, QDateTime, Qt, QTime
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDateEdit, QDateTimeEdit, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
                               QTableWidget, QTableWidgetItem, QTabWidget, QTimeEdit, QVBoxLayout, QWidget)

from dashboard.dashboard_controller import DashboardController
from dashboard.styles import theme
from dashboard.widgets.activity import ScanIndicator
from dashboard.widgets.hud import HudPanel
from services.task_service import PRIORITIES, Task

PRIORITY_COLORS = {"high": theme.RED, "medium": theme.ORANGE, "low": theme.CYAN_DIM}


class TaskDialog(QDialog):
    def __init__(self, parent: QWidget, task: Task | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit task" if task else "New task")
        self.setMinimumWidth(420)
        form = QFormLayout(self)
        self.title = QLineEdit(task.title if task else "")
        self.title.setMaxLength(200)
        self.priority = QComboBox()
        self.priority.addItems(PRIORITIES)
        self.priority.setCurrentText(task.priority if task else "medium")
        self.has_due = QCheckBox("Has due date")
        self.due = QDateEdit()
        self.due.setCalendarPopup(True)
        self.due.setDisplayFormat("yyyy-MM-dd")
        due = task.due_date if task and task.due_date else date.today()
        self.due.setDate(QDate(due.year, due.month, due.day))
        self.has_due.setChecked(bool(task and task.due_date))
        self.due.setEnabled(self.has_due.isChecked())
        self.has_due.toggled.connect(self.due.setEnabled)
        self.has_time = QCheckBox("At a time")
        self.due_time = QTimeEdit()
        self.due_time.setDisplayFormat("HH:mm")
        t = task.due_time if task and task.due_time else time(17, 0)
        self.due_time.setTime(QTime(t.hour, t.minute))
        self.has_time.setChecked(bool(task and task.due_time))
        self.has_remind = QCheckBox("Remind me")
        self.remind = QDateTimeEdit()
        self.remind.setCalendarPopup(True)
        self.remind.setDisplayFormat("yyyy-MM-dd HH:mm")
        r = (task.remind_at if task and task.remind_at
             else datetime.now().replace(second=0, microsecond=0) + timedelta(hours=1))
        self.remind.setDateTime(QDateTime(QDate(r.year, r.month, r.day), QTime(r.hour, r.minute)))
        self.has_remind.setChecked(bool(task and task.remind_at and not task.reminded_at))
        for box, edit in ((self.has_time, self.due_time), (self.has_remind, self.remind)):
            edit.setEnabled(box.isChecked())
            box.toggled.connect(edit.setEnabled)
        self.has_time.setEnabled(self.has_due.isChecked())
        self.has_due.toggled.connect(self.has_time.setEnabled)
        self.notes = QPlainTextEdit(task.notes if task else "")
        self.notes.setFixedHeight(90)
        self.error = QLabel("")
        self.error.setStyleSheet(f"color: {theme.RED};")
        form.addRow("Title", self.title)
        form.addRow("Priority", self.priority)
        form.addRow(self.has_due, self.due)
        form.addRow(self.has_time, self.due_time)
        form.addRow(self.has_remind, self.remind)
        form.addRow("Notes", self.notes)
        form.addRow(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _validate(self) -> None:
        if not self.title.text().strip():
            self.error.setText("A title is required.")
            return
        self.accept()

    def values(self) -> dict:
        d, t, r = self.due.date(), self.due_time.time(), self.remind.dateTime()
        due = self.has_due.isChecked()
        return {
            "title": self.title.text(),
            "priority": self.priority.currentText(),
            "due_date": date(d.year(), d.month(), d.day()) if due else None,
            "due_time": time(t.hour(), t.minute()) if due and self.has_time.isChecked() else None,
            "remind_at": datetime(r.date().year(), r.date().month(), r.date().day(), r.time().hour(),
                                  r.time().minute()) if self.has_remind.isChecked() else None,
            "notes": self.notes.toPlainText(),
        }


class TasksPanel(QWidget):
    def __init__(self, controller: DashboardController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._c = controller
        self._tasks = controller.core.tasks
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        self.activity = ScanIndicator("TASK LINK READY")
        layout.addWidget(self.activity)
        panel = HudPanel("Task matrix")
        layout.addWidget(panel)

        toolbar = QHBoxLayout()
        self.quick = QLineEdit()
        self.quick.setPlaceholderText("Quick add: type a task title and press Enter")
        self.quick.returnPressed.connect(self._quick_add)
        add = QPushButton("+ NEW TASK")
        add.setObjectName("Primary")
        add.clicked.connect(self._new_task)
        self.summary = QLabel("")
        self.summary.setObjectName("HudLabel")
        toolbar.addWidget(self.quick, 1)
        toolbar.addWidget(add)
        panel.body.addLayout(toolbar)
        panel.body.addWidget(self.summary)

        self.tabs = QTabWidget()
        self.pending = self._make_table()
        self.completed = self._make_table()
        self.tabs.addTab(self.pending, "PENDING")
        self.tabs.addTab(self.completed, "COMPLETED")
        panel.body.addWidget(self.tabs, 1)

        actions = QHBoxLayout()
        self.toggle_btn = QPushButton("MARK COMPLETE / INCOMPLETE")
        self.toggle_btn.clicked.connect(self._toggle_selected)
        edit = QPushButton("EDIT")
        edit.clicked.connect(self._edit_selected)
        delete = QPushButton("DELETE")
        delete.setObjectName("Danger")
        delete.clicked.connect(self._delete_selected)
        actions.addWidget(self.toggle_btn)
        actions.addWidget(edit)
        actions.addStretch(1)
        actions.addWidget(delete)
        panel.body.addLayout(actions)
        self.refresh()

    def _make_table(self) -> QTableWidget:
        table = QTableWidget(0, 6)
        table.setHorizontalHeaderLabels(["DONE", "TITLE", "PRIORITY", "DUE", "REMINDER", "NOTES"])
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        header = table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        for col in (0, 2, 3, 4):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        table.itemChanged.connect(self._checkbox_changed)
        table.doubleClicked.connect(lambda _i: self._edit_selected())
        return table

    # -- data -----------------------------------------------------------------------------
    def refresh(self) -> None:
        session = self._c.session
        if session is None:
            return
        self._c.track(self.activity, "RETRIEVING TASKS",
                      lambda: (self._tasks.list(session, False), self._tasks.list(session, True)), self._populate,
                      self._error, done_label="TASKS SYNCHRONISED")

    def clear_view(self) -> None:
        for table in (self.pending, self.completed):
            table.setRowCount(0)
        self.summary.setText("")

    def _populate(self, result) -> None:
        pending, completed = result
        self._fill(self.pending, pending)
        self._fill(self.completed, completed)
        overdue = sum(1 for t in pending if t.is_overdue())
        self.summary.setText(f"{len(pending)} PENDING  ·  {len(completed)} COMPLETED  ·  {overdue} OVERDUE")
        self.tabs.setTabText(0, f"PENDING ({len(pending)})")
        self.tabs.setTabText(1, f"COMPLETED ({len(completed)})")

    def _fill(self, table: QTableWidget, tasks: list[Task]) -> None:
        table.blockSignals(True)
        table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            check.setCheckState(Qt.CheckState.Checked if task.completed else Qt.CheckState.Unchecked)
            check.setData(Qt.ItemDataRole.UserRole, task.id)
            title = QTableWidgetItem(task.title)
            prio = QTableWidgetItem(task.priority.upper())
            prio.setForeground(QColor(PRIORITY_COLORS[task.priority]))
            due = QTableWidgetItem((task.due_date.isoformat() + (f" {task.due_time:%H:%M}" if task.due_time else ""))
                                   if task.due_date else "—")
            if task.is_overdue(datetime.now()):
                due.setForeground(QColor(theme.RED))
                due.setText(due.text() + "  OVERDUE")
            if task.remind_at is None:
                remind = QTableWidgetItem("—")
            else:
                remind = QTableWidgetItem(f"{task.remind_at:%Y-%m-%d %H:%M}" + ("  ✓ sent" if task.reminded_at else ""))
                if task.reminded_at:
                    remind.setForeground(QColor(theme.MUTED))
            notes = QTableWidgetItem(task.notes.replace("\n", " ")[:120])
            for col, item in enumerate((check, title, prio, due, remind, notes)):
                table.setItem(row, col, item)
        table.blockSignals(False)

    def _error(self, message: str) -> None:
        QMessageBox.warning(self, "Tasks", message)

    def _selected_id(self) -> int | None:
        table = self.tabs.currentWidget()
        rows = table.selectionModel().selectedRows()
        if not rows:
            return None
        return table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)

    # -- actions ----------------------------------------------------------------------------
    def _quick_add(self) -> None:
        title = self.quick.text().strip()
        if not title:
            return
        self._c.track(self.activity, "WRITING TASK", lambda: self._tasks.create(self._c.session, title), lambda _t: (self.quick.clear(), self.refresh()), self._error)

    def _new_task(self) -> None:
        dialog = TaskDialog(self)
        if dialog.exec():
            values = dialog.values()
            self._c.track(self.activity, "WRITING TASK", lambda: self._tasks.create(self._c.session, **values), lambda _t: self.refresh(), self._error)

    def _edit_selected(self) -> None:
        task_id = self._selected_id()
        if task_id is None:
            return
        task = self._tasks.get(self._c.session, task_id)
        dialog = TaskDialog(self, task)
        if dialog.exec():
            values = dialog.values()
            self._c.track(self.activity, "WRITING TASK", lambda: self._tasks.update(self._c.session, task_id, **values), lambda _t: self.refresh(), self._error)

    def _toggle_selected(self) -> None:
        task_id = self._selected_id()
        if task_id is None:
            return
        done = self.tabs.currentWidget() is self.pending
        self._c.track(self.activity, "UPDATING TASK", lambda: self._tasks.set_completed(self._c.session, task_id, done), lambda _t: self.refresh(), self._error)

    def _checkbox_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        task_id = item.data(Qt.ItemDataRole.UserRole)
        done = item.checkState() == Qt.CheckState.Checked
        self._c.track(self.activity, "UPDATING TASK", lambda: self._tasks.set_completed(self._c.session, task_id, done), lambda _t: self.refresh(), self._error)

    def _delete_selected(self) -> None:
        task_id = self._selected_id()
        if task_id is None:
            return
        title = self._tasks.get(self._c.session, task_id).title
        answer = QMessageBox.question(self, "Delete task", f"Delete the task “{title}”? This cannot be undone.")
        if answer == QMessageBox.StandardButton.Yes:
            self._c.track(self.activity, "DELETING TASK", lambda: self._tasks.delete(self._c.session, task_id), lambda _r: self.refresh(), self._error)
