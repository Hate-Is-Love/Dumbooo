"""HISTORY page: the signed-in user's saved conversations.

List, open, search (full-text), continue, rename, archive, delete, export and
clear. All work runs on worker threads through ``HistoryService`` with the
session of the signed-in user, while a ScanIndicator shows the live database
activity. Deleting conversations does not delete long-term memories (those are
managed on the MEMORY page) — the confirmation dialogs say so.
"""

from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton, QSplitter, QTextBrowser, QVBoxLayout, QWidget)

from dashboard.styles import theme
from dashboard.widgets.activity import ScanIndicator
from dashboard.widgets.hud import HudPanel

STATUS_COLORS = {"completed": theme.MUTED, "failed": theme.RED, "interrupted": theme.AMBER, "pending": theme.CYAN_DIM}


def render_conversation(conversation: dict) -> str:
    parts = [f"<h3 style='color:{theme.CYAN}; font-family:Consolas; letter-spacing:2px;'>"
             f"{html.escape(conversation['title'])}</h3>"]
    for m in conversation["messages"]:
        who = "YOU" + (" · VOICE" if m["input_mode"] == "voice" else "") if m["role"] == "user" else "DUMBOO"
        color = theme.PALE if m["role"] == "assistant" else theme.TEXT
        meta = m["created_at"].replace("T", " ")
        if m["role"] == "assistant" and m.get("model"):
            meta += f" · {html.escape(str(m['provider']))}/{html.escape(str(m['model']))}"
        if m["status"] != "completed":
            meta += f" · <span style='color:{STATUS_COLORS[m['status']]}'>{m['status'].upper()}" \
                    f"{(' (' + html.escape(m['error_kind']) + ')') if m.get('error_kind') else ''}</span>"
        body = html.escape(m["content"]).replace("\n", "<br>")
        parts.append(f"<p style='margin:10px 0 2px 0; color:{theme.MUTED}; font-family:Consolas; font-size:10px;'>"
                     f"{who} · {meta}</p><div style='color:{color};'>{body}</div>")
    return "".join(parts)


class HistoryPanel(QWidget):
    continue_requested = Signal(dict)   # a conversation (with messages) to continue in the chat

    def __init__(self, controller) -> None:
        super().__init__()
        self._c = controller
        self._history = controller.core.history
        self._current: dict | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        self.activity = ScanIndicator("ARCHIVE LINK READY")
        layout.addWidget(self.activity)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = HudPanel("Conversation archive")
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search your messages…")
        self.search.returnPressed.connect(self.run_search)
        search_btn = QPushButton("SEARCH")
        search_btn.clicked.connect(self.run_search)
        row.addWidget(self.search, 1)
        row.addWidget(search_btn)
        left.body.addLayout(row)
        self.show_archived = QCheckBox("Show archived")
        self.show_archived.toggled.connect(lambda _on: self.refresh())
        left.body.addWidget(self.show_archived)
        self.list = QListWidget()
        self.list.itemClicked.connect(self._item_clicked)
        left.body.addWidget(self.list, 1)
        self.summary = QLabel("")
        self.summary.setObjectName("HudLabel")
        left.body.addWidget(self.summary)
        splitter.addWidget(left)

        right = HudPanel("Transcript")
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        self.view.setPlaceholderText("Select a conversation.")
        right.body.addWidget(self.view, 1)
        actions = QHBoxLayout()
        self.continue_btn = QPushButton("CONTINUE")
        self.continue_btn.setObjectName("Primary")
        self.continue_btn.setToolTip("Continue this conversation in the chat (DUMBOO gets its recent context back)")
        self.continue_btn.clicked.connect(self._continue)
        self.rename_btn = QPushButton("RENAME")
        self.rename_btn.clicked.connect(self._rename)
        self.archive_btn = QPushButton("ARCHIVE")
        self.archive_btn.clicked.connect(self._toggle_archive)
        self.delete_btn = QPushButton("DELETE")
        self.delete_btn.setObjectName("Danger")
        self.delete_btn.clicked.connect(self._delete)
        for b in (self.continue_btn, self.rename_btn, self.archive_btn, self.delete_btn):
            actions.addWidget(b)
        actions.addStretch(1)
        right.body.addLayout(actions)
        bulk = QHBoxLayout()
        self.export_btn = QPushButton("EXPORT MY DATA…")
        self.export_btn.setToolTip("Save all your conversations, memories, preferences, tasks and events as a JSON file")
        self.export_btn.clicked.connect(self._export)
        self.clear_btn = QPushButton("CLEAR ALL HISTORY")
        self.clear_btn.setObjectName("Danger")
        self.clear_btn.clicked.connect(self._clear_all)
        bulk.addWidget(self.export_btn)
        bulk.addStretch(1)
        bulk.addWidget(self.clear_btn)
        right.body.addLayout(bulk)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        right.body.addWidget(self.status)
        splitter.addWidget(right)
        splitter.setSizes([420, 800])
        layout.addWidget(splitter, 1)
        self._set_selected(None)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self.refresh)

    # -- helpers --------------------------------------------------------------------
    def _note(self, text: str, ok: bool = True) -> None:
        self.status.setStyleSheet(f"color: {theme.GREEN if ok else theme.RED};")
        self.status.setText(text)

    def _error(self, text: str) -> None:
        self._note(text, ok=False)

    def _set_selected(self, conversation: dict | None) -> None:
        self._current = conversation
        for b in (self.continue_btn, self.rename_btn, self.archive_btn, self.delete_btn):
            b.setEnabled(conversation is not None)
        if conversation is None:
            self.view.clear()
        else:
            self.view.setHtml(render_conversation(conversation))
            self.archive_btn.setText("UNARCHIVE" if conversation["status"] == "archived" else "ARCHIVE")

    def clear(self) -> None:
        """Called on sign-out: drop everything that was shown."""
        self.list.clear()
        self.search.clear()
        self.summary.setText("")
        self.status.setText("")
        self._set_selected(None)

    def schedule_refresh(self) -> None:
        if self.isVisible():
            self._refresh_timer.start(400)

    # -- loading ----------------------------------------------------------------------
    def refresh(self) -> None:
        if self._c.session is None:
            return
        session, archived = self._c.session, self.show_archived.isChecked()
        self._c.track(self.activity, "RETRIEVING CONVERSATIONS",
                      lambda: self._history.list_conversations(session, include_archived=archived),
                      self._show_list, self._error, done_label="ARCHIVE SYNCHRONISED")

    def _show_list(self, conversations: list[dict]) -> None:
        self.list.clear()
        for conv in conversations:
            mark = "  [archived]" if conv["status"] == "archived" else ""
            item = QListWidgetItem(f"{conv['updated_at'][:16].replace('T', ' ')}   {conv['title']}{mark}\n"
                                   f"      {conv['message_count']} messages")
            item.setData(Qt.ItemDataRole.UserRole, ("conversation", conv["id"]))
            self.list.addItem(item)
        self.summary.setText(f"{len(conversations)} CONVERSATION(S)")
        if not conversations:
            placeholder = QListWidgetItem("No saved conversations yet.")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(placeholder)

    def run_search(self) -> None:
        text = self.search.text().strip()
        if not text:
            self.refresh()
            return
        session = self._c.session
        self._c.track(self.activity, "SEARCHING ARCHIVE", lambda: self._history.search_messages(session, text, 50),
                      self._show_results, self._error, done_label="SEARCH COMPLETE")

    def _show_results(self, hits: list[dict]) -> None:
        self.list.clear()
        for hit in hits:
            who = "YOU" if hit["role"] == "user" else "DUMBOO"
            snippet = " ".join(hit["content"].split())[:110]
            item = QListWidgetItem(f"{hit['created_at'][:16].replace('T', ' ')}  {who}: {snippet}\n      in “{hit['title']}”")
            item.setData(Qt.ItemDataRole.UserRole, ("conversation", hit["conversation_id"]))
            self.list.addItem(item)
        self.summary.setText(f"{len(hits)} MATCHING MESSAGE(S)")
        if not hits:
            placeholder = QListWidgetItem("No messages match your search.")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(placeholder)

    def _item_clicked(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if data:
            self.open_conversation(data[1])

    def open_conversation(self, conversation_id: int) -> None:
        session = self._c.session
        self._c.track(self.activity, "DECRYPTING TRANSCRIPT", lambda: self._history.get_conversation(session, conversation_id),
                      self._set_selected, self._error, done_label="TRANSCRIPT LOADED")

    # -- actions --------------------------------------------------------------------------
    def _continue(self) -> None:
        if self._current:
            self.continue_requested.emit(self._current)

    def _rename(self) -> None:
        if not self._current:
            return
        title, ok = QInputDialog.getText(self, "Rename conversation", "New title:", text=self._current["title"])
        if not ok or not title.strip():
            return
        session, cid = self._c.session, self._current["id"]
        self._c.track(self.activity, "WRITING", lambda: self._history.rename_conversation(session, cid, title),
                      lambda _r: (self.refresh(), self.open_conversation(cid), self._note("Renamed.")), self._error,
                      done_label="SAVED")

    def _toggle_archive(self) -> None:
        if not self._current:
            return
        archive = self._current["status"] != "archived"
        session, cid = self._c.session, self._current["id"]
        self._c.track(self.activity, "ARCHIVING" if archive else "RESTORING",
                      lambda: self._history.set_archived(session, cid, archive),
                      lambda _r: (self.refresh(), self.open_conversation(cid),
                                  self._note("Archived (hidden from the list; tick 'Show archived' to see it)."
                                             if archive else "Moved back to active conversations.")),
                      self._error, done_label="SAVED")

    def _delete(self) -> None:
        if not self._current:
            return
        answer = QMessageBox.question(
            self, "Delete conversation",
            f"Delete “{self._current['title']}” and all its messages? This cannot be undone.\n\n"
            "Facts DUMBOO remembered from it stay in your MEMORY page until you delete them there.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        session, cid = self._c.session, self._current["id"]
        self._c.track(self.activity, "DELETING", lambda: self._history.delete_conversation(session, cid),
                      lambda _r: (self._set_selected(None), self.refresh(), self._note("Conversation deleted.")),
                      self._error, done_label="DELETED")

    def _clear_all(self) -> None:
        answer = QMessageBox.question(
            self, "Clear all history",
            "Delete ALL your saved conversations? This cannot be undone.\n\n"
            "Your saved memories are NOT deleted by this (manage them on the MEMORY page).")
        if answer != QMessageBox.StandardButton.Yes:
            return
        session = self._c.session
        self._c.track(self.activity, "PURGING ARCHIVE", lambda: self._history.clear_history(session, confirmed=True),
                      lambda n: (self._set_selected(None), self.refresh(), self._note(f"Deleted {n} conversation(s).")),
                      self._error, done_label="ARCHIVE CLEARED")

    def _export(self, path: str | None = None) -> None:
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, "Export my data", str(Path.home() / "dumboo-export.json"),
                                                  "JSON (*.json)")
        if not path:
            return
        session = self._c.session
        recorder = self._c.core.recorder

        def work() -> int:
            recorder.flush()  # include the very latest messages
            data = self._history.export_json(session)
            Path(path).write_text(data, encoding="utf-8")
            return len(data)

        self._c.track(self.activity, "EXPORTING", work,
                      lambda size: self._note(f"Exported {size / 1024:.1f} KB to {path}. Keep this file private: it "
                                              "contains your conversations."), self._error, done_label="EXPORT COMPLETE")
