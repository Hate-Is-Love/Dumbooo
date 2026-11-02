"""Schedule page: today's and upcoming events with reminders."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from PySide6.QtCore import QDate, Qt, QTime
from PySide6.QtWidgets import (QCheckBox, QDateEdit, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
                               QSpinBox, QTimeEdit, QVBoxLayout, QWidget)

from dashboard.dashboard_controller import DashboardController
from dashboard.styles import theme
from dashboard.widgets.activity import ScanIndicator
from dashboard.widgets.hud import HudPanel
from services.schedule_service import ScheduleEvent, parse_start


class EventDialog(QDialog):
    def __init__(self, parent: QWidget, event: ScheduleEvent | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit event" if event else "New event")
        self.setMinimumWidth(440)
        form = QFormLayout(self)
        start = event.starts_at if event else (datetime.now() + timedelta(hours=1)).replace(minute=0)
        self.title = QLineEdit(event.title if event else "")
        self.title.setMaxLength(200)
        self.day = QDateEdit(QDate(start.year, start.month, start.day))
        self.day.setCalendarPopup(True)
        self.day.setDisplayFormat("yyyy-MM-dd")
        self.time = QTimeEdit(QTime(start.hour, start.minute))
        self.time.setDisplayFormat("HH:mm")
        self.remind = QCheckBox("Remind me")
        self.remind_minutes = QSpinBox()
        self.remind_minutes.setRange(0, 7 * 24 * 60)
        self.remind_minutes.setSuffix(" min before")
        minutes = event.reminder_minutes if event else 10
        self.remind.setChecked(minutes is not None)
        self.remind_minutes.setValue(minutes if minutes is not None else 10)
        self.remind_minutes.setEnabled(self.remind.isChecked())
        self.remind.toggled.connect(self.remind_minutes.setEnabled)
        self.description = QPlainTextEdit(event.description if event else "")
        self.description.setFixedHeight(90)
        self.error = QLabel("")
        self.error.setStyleSheet(f"color: {theme.RED};")
        self.error.setWordWrap(True)
        self._editing = event is not None
        form.addRow("Title", self.title)
        form.addRow("Date", self.day)
        form.addRow("Time (24h)", self.time)
        form.addRow(self.remind, self.remind_minutes)
        form.addRow("Description", self.description)
        form.addRow(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def starts_at(self) -> datetime:
        d, t = self.day.date(), self.time.time()
        return parse_start(date(d.year(), d.month(), d.day()), f"{t.hour():02d}:{t.minute():02d}")

    def _validate(self) -> None:
        if not self.title.text().strip():
            self.error.setText("A title is required.")
            return
        if not self._editing and self.starts_at() < datetime.now() - timedelta(minutes=1):
            answer = QMessageBox.question(self, "Event in the past", "This time is in the past. Save it anyway?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.accept()

    def values(self) -> dict:
        return {
            "title": self.title.text(),
            "starts_at": self.starts_at(),
            "description": self.description.toPlainText(),
            "reminder_minutes": self.remind_minutes.value() if self.remind.isChecked() else None,
        }


def _describe(event: ScheduleEvent, show_date: bool) -> str:
    when = event.starts_at.strftime("%a %d %b  %H:%M" if show_date else "%H:%M")
    reminder = f"   ⏰ {event.reminder_minutes} min" if event.reminder_minutes is not None else ""
    done = "  (reminded)" if event.reminded_at else ""
    desc = f"\n      {event.description[:90]}" if event.description else ""
    return f"{when}   {event.title}{reminder}{done}{desc}"


class SchedulePanel(QWidget):
    def __init__(self, controller: DashboardController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._c = controller
        self._schedule = controller.core.schedule
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        self.activity = ScanIndicator("SCHEDULE LINK READY")
        outer.addWidget(self.activity)
        layout = QHBoxLayout()
        layout.setSpacing(14)
        outer.addLayout(layout, 1)

        today = HudPanel("Today")
        self.today_label = QLabel("")
        self.today_label.setObjectName("BigValue")
        today.body.addWidget(self.today_label)
        self.today_list = self._make_list()
        today.body.addWidget(self.today_list, 1)
        layout.addWidget(today, 1)

        upcoming = HudPanel("Upcoming · 30 days")
        self.upcoming_list = self._make_list()
        upcoming.body.addWidget(self.upcoming_list, 1)
        layout.addWidget(upcoming, 1)

        side = QVBoxLayout()
        add = QPushButton("+ NEW EVENT")
        add.setObjectName("Primary")
        add.clicked.connect(self._new_event)
        edit = QPushButton("EDIT SELECTED")
        edit.clicked.connect(self._edit_selected)
        delete = QPushButton("DELETE SELECTED")
        delete.setObjectName("Danger")
        delete.clicked.connect(self._delete_selected)
        self.reminder_state = QLabel("")
        self.reminder_state.setWordWrap(True)
        self.reminder_state.setObjectName("HudLabel")
        for w in (add, edit, delete):
            side.addWidget(w)
        side.addSpacing(20)
        side.addWidget(self.reminder_state)
        side.addStretch(1)
        layout.addLayout(side)
        self.refresh()

    def clear_view(self) -> None:
        self.today_list.clear()
        self.upcoming_list.clear()

    def _make_list(self) -> QListWidget:
        lw = QListWidget()
        lw.itemDoubleClicked.connect(lambda _i: self._edit_selected())
        lw.itemSelectionChanged.connect(lambda: self._sync_selection(lw))
        return lw

    def _sync_selection(self, source: QListWidget) -> None:
        if source.selectedItems():
            other = self.upcoming_list if source is self.today_list else self.today_list
            other.blockSignals(True)
            other.clearSelection()
            other.blockSignals(False)

    def refresh(self) -> None:
        self.today_label.setText(datetime.now().strftime("%A %d %B"))
        running = self._c.core.reminders.running
        self.reminder_state.setText(
            "REMINDER SERVICE: RUNNING\nAlerts appear in the dashboard and as Windows tray notifications."
            if running else "REMINDER SERVICE: STOPPED\nReminders will not be delivered.")
        session = self._c.session
        if session is None:
            return
        self._c.track(self.activity, "RETRIEVING SCHEDULE",
                      lambda: (self._schedule.today(session), self._schedule.upcoming(session, 30)), self._populate,
                          lambda m: QMessageBox.warning(self, "Schedule", m))

    def _populate(self, result) -> None:
        today, upcoming = result
        for lw, events, show_date in ((self.today_list, today, False), (self.upcoming_list, upcoming, True)):
            lw.clear()
            if not events:
                placeholder = QListWidgetItem("No events")
                placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
                lw.addItem(placeholder)
            for event in events:
                item = QListWidgetItem(_describe(event, show_date))
                item.setData(Qt.ItemDataRole.UserRole, event.id)
                if event.starts_at < datetime.now():
                    item.setForeground(theme.qcolor(theme.MUTED))
                lw.addItem(item)

    def _selected_id(self) -> int | None:
        for lw in (self.today_list, self.upcoming_list):
            items = lw.selectedItems()
            if items and items[0].data(Qt.ItemDataRole.UserRole) is not None:
                return items[0].data(Qt.ItemDataRole.UserRole)
        return None

    def _new_event(self) -> None:
        dialog = EventDialog(self)
        if dialog.exec():
            values = dialog.values()
            self._c.track(self.activity, "WRITING EVENT", lambda: self._schedule.create(self._c.session, **values), lambda _e: self.refresh(),
                              lambda m: QMessageBox.warning(self, "Schedule", m))

    def _edit_selected(self) -> None:
        event_id = self._selected_id()
        if event_id is None:
            return
        dialog = EventDialog(self, self._schedule.get(self._c.session, event_id))
        if dialog.exec():
            values = dialog.values()
            self._c.track(self.activity, "WRITING EVENT", lambda: self._schedule.update(self._c.session, event_id, **values), lambda _e: self.refresh(),
                              lambda m: QMessageBox.warning(self, "Schedule", m))

    def _delete_selected(self) -> None:
        event_id = self._selected_id()
        if event_id is None:
            return
        title = self._schedule.get(self._c.session, event_id).title
        if QMessageBox.question(self, "Delete event", f"Delete “{title}”? This cannot be undone.") \
                == QMessageBox.StandardButton.Yes:
            self._c.track(self.activity, "DELETING EVENT", lambda: self._schedule.delete(self._c.session, event_id), lambda _r: self.refresh(),
                              lambda m: QMessageBox.warning(self, "Schedule", m))
