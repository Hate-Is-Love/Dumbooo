"""MEMORY page: what DUMBOO remembers about the signed-in user.

View (with category, confidence, timestamps), search, add, edit, delete, clear
and the change log. Every action goes through ``MemoryService`` with the
signed-in user's session; a ScanIndicator shows the live database work.
"""

from __future__ import annotations

import json

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from dashboard.styles import theme
from dashboard.widgets.activity import ScanIndicator
from dashboard.widgets.hud import HudPanel
from services.memory_service import CATEGORIES, NeedsConfirmation

VERIFICATION_TEXT = {"user_confirmed": "confirmed by you", "user_stated": "you mentioned it", "inferred": "inferred"}


class MemoryDialog(QDialog):
    def __init__(self, parent: QWidget, memory: dict | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit memory" if memory else "Add memory")
        self.setMinimumWidth(460)
        form = QFormLayout(self)
        self.key = QLineEdit(memory["memory_key"] if memory else "")
        self.key.setMaxLength(80)
        self.key.setPlaceholderText("e.g. project")
        self.value = QPlainTextEdit(memory["memory_value"] if memory else "")
        self.value.setFixedHeight(80)
        self.value.setPlaceholderText("e.g. DUMBOO")
        self.category = QComboBox()
        self.category.addItems(CATEGORIES)
        self.category.setCurrentText(memory["category"] if memory else "fact")
        note = QLabel("Do not store passwords or other secrets: DUMBOO refuses them.")
        note.setObjectName("HudLabel")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow("Subject", self.key)
        form.addRow("Value", self.value)
        form.addRow("Category", self.category)
        form.addRow(note)
        form.addRow(buttons)

    def values(self) -> tuple[str, str, str]:
        return self.key.text().strip(), self.value.toPlainText().strip(), self.category.currentText()


class MemoryPanel(QWidget):
    COLUMNS = ["SUBJECT", "VALUE", "CATEGORY", "SOURCE", "CONF.", "SAVED", "LAST USED", "STATUS"]

    def __init__(self, controller) -> None:
        super().__init__()
        self._c = controller
        self._memory = controller.core.memory
        self._rows: list[dict] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        self.activity = ScanIndicator("MEMORY CORE READY")
        layout.addWidget(self.activity)
        panel = HudPanel("Long-term memory")
        layout.addWidget(panel, 1)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search memories…")
        self.search.returnPressed.connect(self.refresh)
        self.category = QComboBox()
        self.category.addItem("all categories", None)
        for c in CATEGORIES:
            self.category.addItem(c, c)
        self.category.currentIndexChanged.connect(lambda _i: self.refresh())
        self.show_old = QCheckBox("Show replaced versions")
        self.show_old.toggled.connect(lambda _on: self.refresh())
        find = QPushButton("SEARCH")
        find.clicked.connect(self.refresh)
        filters.addWidget(self.search, 1)
        filters.addWidget(find)
        filters.addWidget(self.category)
        filters.addWidget(self.show_old)
        panel.body.addLayout(filters)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col in (0, 2, 3, 4, 5, 6, 7):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.doubleClicked.connect(lambda _i: self._edit())
        panel.body.addWidget(self.table, 1)

        actions = QHBoxLayout()
        add = QPushButton("+ ADD")
        add.setObjectName("Primary")
        add.clicked.connect(self._add)
        self.edit_btn = QPushButton("EDIT")
        self.edit_btn.clicked.connect(self._edit)
        self.delete_btn = QPushButton("DELETE")
        self.delete_btn.setObjectName("Danger")
        self.delete_btn.clicked.connect(self._delete)
        audit = QPushButton("CHANGE LOG")
        audit.clicked.connect(self._show_audit)
        clear = QPushButton("CLEAR ALL MEMORIES")
        clear.setObjectName("Danger")
        clear.clicked.connect(self._clear_all)
        for b in (add, self.edit_btn, self.delete_btn, audit):
            actions.addWidget(b)
        actions.addStretch(1)
        actions.addWidget(clear)
        panel.body.addLayout(actions)
        self.summary = QLabel("")
        self.summary.setObjectName("HudLabel")
        self.status = QLabel("")
        self.status.setWordWrap(True)
        hint = QLabel("You can also say: “remember that my project is called DUMBOO”, “what do you remember about "
                      "me?”, “forget my favourite colour”, “clear my memories”.")
        hint.setWordWrap(True)
        hint.setObjectName("HudLabel")
        panel.body.addWidget(self.summary)
        panel.body.addWidget(self.status)
        panel.body.addWidget(hint)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self.refresh)

    # -- helpers ----------------------------------------------------------------------
    def _note(self, text: str, ok: bool = True) -> None:
        self.status.setStyleSheet(f"color: {theme.GREEN if ok else theme.RED};")
        self.status.setText(text)

    def _error(self, text: str) -> None:
        self._note(text, ok=False)

    def clear(self) -> None:
        self.table.setRowCount(0)
        self._rows = []
        self.summary.setText("")
        self.status.setText("")
        self.search.clear()

    def schedule_refresh(self) -> None:
        if self.isVisible():
            self._refresh_timer.start(300)

    def _selected(self) -> dict | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        row = self._rows[rows[0].row()]
        return row if row["status"] == "active" else None

    # -- loading -------------------------------------------------------------------------
    def refresh(self) -> None:
        session = self._c.session
        if session is None:
            return
        text, category, old = self.search.text().strip(), self.category.currentData(), self.show_old.isChecked()

        def load() -> list[dict]:
            if text:
                rows = self._memory.search(session, text, limit=100)
                return [r for r in rows if not category or r["category"] == category]
            return self._memory.list(session, include_superseded=old, category=category)

        self._c.track(self.activity, "SCANNING MEMORY CORE", load, self._show, self._error,
                      done_label="MEMORY SYNCHRONISED")

    def _show(self, rows: list[dict]) -> None:
        self._rows = rows
        self.table.setRowCount(len(rows))
        for r, m in enumerate(rows):
            values = [m["memory_key"], m["memory_value"], m["category"],
                      VERIFICATION_TEXT.get(m["verification_status"], m["verification_status"]),
                      f"{m['confidence']:.0%}", m["updated_at"][:16].replace("T", " "),
                      (m["last_accessed_at"] or "never")[:16].replace("T", " "),
                      m["status"] + (" · sensitive" if m["sensitivity"] == "sensitive" else "")]
            for c, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if m["status"] != "active":
                    item.setForeground(theme.qcolor(theme.MUTED))
                self.table.setItem(r, c, item)
        active = sum(1 for m in rows if m["status"] == "active")
        self.summary.setText(f"{active} ACTIVE MEMOR{'Y' if active == 1 else 'IES'} SHOWN")

    # -- actions ----------------------------------------------------------------------------
    def _save(self, key: str, value: str, category: str, memory_id: int | None, sensitive_ok: bool = False) -> None:
        session = self._c.session

        def work():
            if memory_id is None:
                return self._memory.remember(session, key, value, category, verification="user_confirmed",
                                             sensitive_confirmed=sensitive_ok)
            return self._memory.update(session, memory_id, key=key, value=value, category=category)

        def failed(exc: BaseException) -> None:
            if isinstance(exc, NeedsConfirmation):
                if QMessageBox.question(self, "Sensitive information",
                                        "This looks like sensitive personal information (health, finances, beliefs "
                                        "…). Save it anyway?") == QMessageBox.StandardButton.Yes:
                    self._save(key, value, category, memory_id, sensitive_ok=True)
                return
            from dashboard.dashboard_controller import friendly_message
            self._error(friendly_message(exc))

        self._c.track(self.activity, "WRITING MEMORY", work,
                      lambda _r: (self.refresh(), self._note("Memory saved.")), on_exception=failed,
                      done_label="MEMORY WRITTEN")

    def _add(self) -> None:
        dialog = MemoryDialog(self)
        if dialog.exec():
            self._save(*dialog.values(), None)

    def _edit(self) -> None:
        memory = self._selected()
        if memory is None:
            self._error("Select an active memory first.")
            return
        dialog = MemoryDialog(self, memory)
        if dialog.exec():
            self._save(*dialog.values(), memory["id"])

    def _delete(self) -> None:
        memory = self._selected()
        if memory is None:
            self._error("Select an active memory first.")
            return
        if QMessageBox.question(self, "Delete memory",
                                f"Forget “{memory['memory_key']}: {memory['memory_value']}”? Its text is erased; "
                                "older versions of it are erased too.") != QMessageBox.StandardButton.Yes:
            return
        session = self._c.session
        self._c.track(self.activity, "ERASING MEMORY", lambda: self._memory.forget(session, memory["id"]),
                      lambda _r: (self.refresh(), self._note("Memory deleted.")), self._error, done_label="ERASED")

    def _clear_all(self) -> None:
        if QMessageBox.question(self, "Clear all memories",
                                "Erase EVERYTHING DUMBOO remembers about you? This cannot be undone. "
                                "Your conversation history is not affected.") != QMessageBox.StandardButton.Yes:
            return
        session = self._c.session
        self._c.track(self.activity, "PURGING MEMORY CORE", lambda: self._memory.clear_all(session, confirmed=True),
                      lambda n: (self.refresh(), self._note(f"Erased {n} memories.")), self._error,
                      done_label="MEMORY CLEARED")

    def _show_audit(self) -> None:
        session = self._c.session

        def show(entries: list[dict]) -> None:
            lines = []
            for e in entries:
                meta = json.loads(e["metadata"] or "{}")
                details = ", ".join(f"{k}={v}" for k, v in meta.items() if v is not None)
                lines.append(f"{e['timestamp'].replace('T', ' ')}  {e['action']:<10} memory #{e['memory_id'] or '-'}  {details}")
            box = QMessageBox(self)
            box.setWindowTitle("Memory change log")
            box.setText("Recent changes to your memories (no content is kept here):")
            box.setDetailedText("\n".join(lines) or "No changes yet.")
            box.exec()

        self._c.track(self.activity, "READING CHANGE LOG", lambda: self._memory.audit(session, 200), show, self._error,
                      done_label="LOG LOADED")
