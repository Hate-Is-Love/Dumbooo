"""RESEARCH page (Module 5): search box, live progress, answer with clickable citations, sources,
history, saved findings and research settings.

Requests typed here are sent through the same conversation path as chat and voice
(so they appear in the chat and history too); the page then shows the finished record
from the research history. Progress comes only from real ``research.progress`` events.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QSplitter,
                               QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser, QVBoxLayout, QWidget)

from dashboard.styles import theme
from dashboard.widgets.activity import ScanIndicator
from dashboard.widgets.hud import HudPanel, hud_label

MODES = [("Auto", "{q}"), ("Web search", "search for {q}"), ("Latest news", "latest news about {q}"),
         ("Deep research", "research {q}"), ("Fact-check", "verify: {q}")]
STEP_COLORS = {"completed": theme.GREEN, "running": theme.CYAN, "pending": theme.MUTED, "skipped": theme.MUTED,
               "failed": theme.RED, "cancelled": theme.AMBER}
STATUS_COLORS = {"completed": theme.GREEN, "partial": theme.AMBER, "failed": theme.RED, "cancelled": theme.MUTED,
                 "running": theme.CYAN}


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


class ResearchPanel(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self._c = controller
        self._core = controller.core
        self._record: dict | None = None
        self._history: list[dict] = []
        self._saved: list[dict] = []
        self._last_request = ""
        self._running_id: int | None = None
        self.waiting = False   # a request sent from this page hasn't been answered yet
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        top = QHBoxLayout()
        self.activity = ScanIndicator("RESEARCH LINK READY")
        top.addWidget(self.activity, 1)
        self.cancel_button = QPushButton("■  CANCEL RESEARCH")
        self.cancel_button.setObjectName("Danger")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        top.addWidget(self.cancel_button)
        layout.addLayout(top)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_research_tab(), "RESEARCH")
        self.tabs.addTab(self._build_history_tab(), "HISTORY")
        self.tabs.addTab(self._build_saved_tab(), "SAVED FINDINGS")
        self.tabs.addTab(self._build_settings_tab(), "SETTINGS")
        self.tabs.currentChanged.connect(lambda _i: self.refresh())
        layout.addWidget(self.tabs, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self.refresh)

    # -- building ---------------------------------------------------------------------------------
    def _build_research_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        box = HudPanel("Ask")
        row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("e.g. latest AI developments · compare Python GUI frameworks · verify: …")
        self.query.returnPressed.connect(self.run)
        self.mode = QComboBox()
        for label, _template in MODES:
            self.mode.addItem(label)
        run = QPushButton("⌕  RESEARCH")
        run.setObjectName("Primary")
        run.clicked.connect(self.run)
        self.retry_button = QPushButton("↻  RETRY")
        self.retry_button.setEnabled(False)
        self.retry_button.clicked.connect(self.retry)
        row.addWidget(self.query, 1)
        row.addWidget(self.mode)
        row.addWidget(run)
        row.addWidget(self.retry_button)
        box.body.addLayout(row)
        self.stage_line = QLabel("")
        self.stage_line.setObjectName("HudLabel")
        self.stage_line.setTextFormat(Qt.TextFormat.RichText)
        box.body.addWidget(self.stage_line)
        self.detail_line = hud_label("Voice works too: “Hey DUMBOO, what are the latest AI developments?”")
        box.body.addWidget(self.detail_line)
        v.addWidget(box)
        split = QSplitter(Qt.Orientation.Horizontal)
        answer_panel = HudPanel("Answer")
        self.answer = QTextBrowser()
        self.answer.setOpenExternalLinks(True)   # clicking a citation opens it in your normal browser
        self._style_links(self.answer)
        answer_panel.body.addWidget(self.answer, 1)
        split.addWidget(answer_panel)
        sources_panel = HudPanel("Sources")
        self.sources = _table(["#", "SOURCE", "SITE", "PUBLISHED", "CONTENT"], 1)
        self.sources.itemSelectionChanged.connect(self._show_source)
        sources_panel.body.addWidget(self.sources, 2)
        self.source_detail = QTextBrowser()
        self.source_detail.setOpenExternalLinks(True)
        self._style_links(self.source_detail)
        sources_panel.body.addWidget(self.source_detail, 1)
        split.addWidget(sources_panel)
        split.setSizes([760, 560])
        v.addWidget(split, 1)
        return page

    def _build_history_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        row = QHBoxLayout()
        self.history_search = QLineEdit()
        self.history_search.setPlaceholderText("Search earlier research…")
        self.history_search.returnPressed.connect(self.refresh)
        find = QPushButton("SEARCH")
        find.clicked.connect(self.refresh)
        row.addWidget(self.history_search, 1)
        row.addWidget(find)
        v.addLayout(row)
        self.history_table = _table(["#", "DATE", "KIND", "QUESTION", "STATUS", "SAVED", "NOTES"], 3)
        self.history_table.doubleClicked.connect(lambda _i: self._open_history())
        v.addWidget(self.history_table, 1)
        row = QHBoxLayout()
        for label, slot, danger in (("OPEN", self._open_history, False), ("REFRESH WITH CURRENT SOURCES",
                                                                           self._refresh_history, False),
                                    ("SAVE FINDINGS TO MEMORY", self._save_history, False),
                                    ("EDIT NOTES", self._edit_notes, False), ("EXPORT .MD", self._export_history, False),
                                    ("DELETE", self._delete_history, True)):
            b = QPushButton(label)
            if danger:
                b.setObjectName("Danger")
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        clear = QPushButton("CLEAR HISTORY")
        clear.setObjectName("Danger")
        clear.clicked.connect(self._clear_history)
        row.addWidget(clear)
        v.addLayout(row)
        return page

    def _build_saved_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        panel = HudPanel("Findings you asked DUMBOO to remember (your long-term memory)")
        self.saved_table = _table(["SUBJECT", "SAVED FINDING", "SAVED"], 1)
        panel.body.addWidget(self.saved_table, 1)
        row = QHBoxLayout()
        edit = QPushButton("CORRECT")
        edit.clicked.connect(self._edit_saved)
        delete = QPushButton("DELETE")
        delete.setObjectName("Danger")
        delete.clicked.connect(self._delete_saved)
        row.addWidget(edit)
        row.addWidget(delete)
        row.addStretch(1)
        panel.body.addLayout(row)
        panel.body.addWidget(hud_label("Say “remember these findings for my DUMBOO project” after a research answer."))
        v.addWidget(panel, 1)
        return page

    def _build_settings_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        panel = HudPanel("Search providers and limits")
        form = QFormLayout()
        self.web_provider = QComboBox()
        self.web_provider.addItems(["auto", "tavily", "brave", "browser", "off"])
        self.news_provider = QComboBox()
        self.news_provider.addItems(["auto", "tavily", "brave", "google_news", "off"])
        self.keyless = QCheckBox("Use DUMBOO's browser for web search when no API key is set")
        self.auto_research = QCheckBox("Research questions about current events automatically")
        self.robots = QCheckBox("Respect robots.txt (don't read pages sites ask bots not to read)")
        self.max_searches = QSpinBox()
        self.max_searches.setRange(1, 20)
        self.max_pages = QSpinBox()
        self.max_pages.setRange(1, 20)
        self.timeout = QSpinBox()
        self.timeout.setRange(20, 600)
        self.timeout.setSuffix(" s")
        self.news_days = QSpinBox()
        self.news_days.setRange(1, 60)
        self.news_days.setSuffix(" days")
        self.region = QLineEdit()
        self.region.setMaxLength(2)
        self.keys = QLabel("")
        self.keys.setWordWrap(True)
        form.addRow("Web search", self.web_provider)
        form.addRow("News", self.news_provider)
        form.addRow(self.keyless)
        form.addRow(self.auto_research)
        form.addRow(self.robots)
        form.addRow("Max searches per request", self.max_searches)
        form.addRow("Max pages read per request", self.max_pages)
        form.addRow("Time limit", self.timeout)
        form.addRow("Default news period", self.news_days)
        form.addRow("Region (country code)", self.region)
        form.addRow("API keys", self.keys)
        panel.body.addLayout(form)
        self.settings_save = QPushButton("SAVE RESEARCH SETTINGS")
        self.settings_save.setObjectName("Primary")
        self.settings_save.clicked.connect(self._save_settings)
        panel.body.addWidget(self.settings_save)
        panel.body.addStretch(1)
        v.addWidget(panel, 1)
        return page

    @staticmethod
    def _style_links(browser: QTextBrowser) -> None:
        from PySide6.QtGui import QPalette

        palette = browser.palette()
        palette.setColor(QPalette.ColorRole.Link, theme.qcolor(theme.CYAN))
        palette.setColor(QPalette.ColorRole.LinkVisited, theme.qcolor(theme.VIOLET))
        browser.setPalette(palette)

    @staticmethod
    def set_markdown(browser: QTextBrowser, markdown: str) -> None:
        """Render Markdown with readable (cyan) links on the dark theme (same renderer as the chat)."""
        from dashboard.widgets.conversation_panel import markdown_html

        browser.setHtml(markdown_html(markdown))

    # -- helpers --------------------------------------------------------------------------------------
    def _note(self, text: str, ok: bool = True) -> None:
        self.status.setStyleSheet(f"color: {theme.GREEN if ok else theme.RED};")
        self.status.setText(text)

    def _error(self, text: str) -> None:
        self._note(text, ok=False)

    def apply_access(self, permissions) -> None:
        self.settings_save.setEnabled("settings.system" in set(permissions))

    def clear(self) -> None:
        self._record = None
        self._history, self._saved = [], []
        self.answer.clear()
        self.sources.setRowCount(0)
        self.source_detail.clear()
        self.history_table.setRowCount(0)
        self.saved_table.setRowCount(0)
        self.stage_line.setText("")
        self.status.setText("")

    def schedule_refresh(self) -> None:
        if self.isVisible():
            self._refresh_timer.start(300)

    # -- running -----------------------------------------------------------------------------------------
    def run(self) -> None:
        q = self.query.text().strip()
        if not q:
            return
        template = MODES[self.mode.currentIndex()][1]
        text = template.format(q=q)
        if self._c.send_text(text):
            self._last_request = text
            self.waiting = True
            self.retry_button.setEnabled(True)
            self._note("Researching… the answer also appears in the chat.")

    def retry(self) -> None:
        if self._last_request and self._c.send_text(self._last_request):
            self.waiting = True

    def not_research(self) -> None:
        """The request was answered without live research (normal chat, a command or a question)."""
        self.waiting = False
        self._note("That was answered without a web search — see the chat. Choose “Web search” or “Latest news” "
                   "to force a live search.", ok=False)

    def cancel(self) -> None:
        research = self._core.research
        self._c.run_async(lambda: research.cancel(), lambda n: self._note(f"Cancel requested ({n} running)."),
                          self._error)

    def on_progress(self, task: dict) -> None:
        status = task["status"]
        chips = []
        for step in task["steps"]:
            color = STEP_COLORS.get(step["status"], theme.MUTED)
            mark = {"completed": "✓", "running": "◔", "failed": "✗", "cancelled": "–", "skipped": "–"}.get(
                step["status"], "○")
            chips.append(f'<span style="color:{color}">{mark} {step["description"]}</span>')
        self.stage_line.setText("  ·  ".join(chips))
        self.detail_line.setText(f"{task['kind'].upper()} · {task['title'][:80]} · {task['detail'][:90]}  "
                                 f"({task['searches']} search(es), {task['pages']} page(s))")
        running = status == "running"
        self.cancel_button.setEnabled(running)
        if running and not self.activity.active:
            self.activity.begin("RESEARCHING")
            self._running_id = task.get("id")
        elif not running and self.activity.active:
            self.activity.end(status in ("completed", "partial"), status.upper())

    def on_done(self, payload: dict) -> None:
        self.waiting = False
        self.cancel_button.setEnabled(False)
        status = payload.get("status", "")
        self._note({"completed": "Research complete.", "partial": "Research finished with limitations (see the notes).",
                    "failed": "The research failed — see the answer for the reason. Use RETRY to try again.",
                    "cancelled": "Research cancelled."}.get(status, status), ok=status in ("completed", "partial"))
        rid = payload.get("research_id")
        if rid is None or self._c.session is None:
            return
        session, history = self._c.session, self._core.research_history
        self._c.run_async(lambda: history.get(session, rid), self.show_record, self._error)
        self.schedule_refresh()

    def show_record(self, record: dict) -> None:
        self._record = record
        header = (f"_{record['kind'].upper()} · {record['status'].upper()} · "
                  f"{record['created_at'][:16].replace('T', ' ')} · “{record['query'][:120]}”_\n\n")
        self.set_markdown(self.answer, header + (record.get("answer") or record.get("error") or "(no answer)"))
        sources = record.get("sources", [])
        self.sources.setRowCount(len(sources))
        for r, s in enumerate(sources):
            values = [str(s["id"]), s["title"], s["domain"], (s.get("published") or "unknown")[:10],
                      "page read" if s.get("read") else "snippet only"]
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                if c == 4:
                    item.setForeground(theme.qcolor(theme.GREEN if s.get("read") else theme.AMBER))
                self.sources.setItem(r, c, item)
        self.source_detail.clear()
        self.tabs.setCurrentIndex(0)

    def _show_source(self) -> None:
        rows = self.sources.selectionModel().selectedRows()
        if not rows or self._record is None:
            return
        s = self._record["sources"][rows[0].row()]
        lines = [f"**[{s['title']}]({s['url']})**", f"{s['domain']} · {s.get('source_name') or ''}",
                 f"Published: {(s.get('published') or 'unknown')[:16]} ({s.get('date_basis')}) · kind: "
                 f"{s.get('quality_label')}",
                 f"Retrieved: {(s.get('retrieved_at') or '—')[:16]} · {'page read' if s.get('read') else 'snippet only'}"
                 + (f" — {s['read_error']}" if s.get("read_error") else ""), "",
                 f"> {s.get('extract') or s.get('snippet') or '(no text)'}"]
        self.set_markdown(self.source_detail, "\n\n".join(lines))

    # -- loading tabs ---------------------------------------------------------------------------------------
    def refresh(self) -> None:
        session = self._c.session
        if session is None:
            return
        tab = self.tabs.currentIndex()
        core = self._core
        if tab == 1:
            text = self.history_search.text().strip()
            self._c.track(self.activity, "LOADING RESEARCH HISTORY",
                          lambda: core.research_history.list(session, text, 200), self._show_history, self._error,
                          done_label="HISTORY SYNCHRONISED")
        elif tab == 2:
            self._c.track(self.activity, "RETRIEVING SAVED FINDINGS",
                          lambda: [m for m in core.memory.list(session) if "research:" in m["memory_key"].lower()],
                          self._show_saved, self._error, done_label="MEMORY SYNCHRONISED")
        elif tab == 3:
            self._c.run_async(lambda: (core.settings.section("research"), core.research_config()), self._show_settings,
                              self._error)

    def _show_history(self, rows: list[dict]) -> None:
        self._history = rows
        self.history_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            values = [str(row["id"]), row["created_at"][:16].replace("T", " "), row["kind"], row["query"],
                      row["status"], "yes" if row["memory_id"] else "", row["user_notes"][:60]]
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                if c == 4:
                    item.setForeground(theme.qcolor(STATUS_COLORS.get(row["status"], theme.TEXT)))
                self.history_table.setItem(r, c, item)

    def _selected(self) -> dict | None:
        rows = self.history_table.selectionModel().selectedRows()
        if not rows:
            self._error("Select a research record first.")
            return None
        return self._history[rows[0].row()]

    def _open_history(self) -> None:
        row = self._selected()
        if row:
            session, history = self._c.session, self._core.research_history
            self._c.track(self.activity, "OPENING RESEARCH", lambda: history.get(session, row["id"]), self.show_record,
                          self._error, done_label="RECORD LOADED")

    def _refresh_history(self) -> None:
        row = self._selected()
        if row and self._c.send_text(f"refresh my previous research about {row['query'][:200]}"):
            self._note("Refreshing with current sources… the result appears in the chat and here.")

    def _save_history(self) -> None:
        row = self._selected()
        if row is None:
            return
        session, core = self._c.session, self._core

        def work():
            record = core.research_history.get(session, row["id"])
            sites = ", ".join(dict.fromkeys(s["domain"] for s in record["sources"][:4]))
            value = f"{record['summary'][:380]} (sources: {sites[:80]}; researched {record['created_at'][:10]})"
            memory = core.research.memory_context.remember_findings(session, f"research: {record['query'][:60]}",
                                                                    value, "fact")
            core.research_history.link_memory(session, record["id"], memory["id"])
            return memory

        self._c.track(self.activity, "WRITING MEMORY", work,
                      lambda m: (self.refresh(), self._note(f"Saved as “{m['memory_key']}”.")), self._error,
                      done_label="MEMORY WRITTEN")

    def _edit_notes(self) -> None:
        row = self._selected()
        if row is None:
            return
        text, ok = QInputDialog.getMultiLineText(self, "Research notes", "Your notes (searchable):", row["user_notes"])
        if not ok:
            return
        session, history = self._c.session, self._core.research_history
        self._c.run_async(lambda: history.set_notes(session, row["id"], text),
                          lambda _r: (self.refresh(), self._note("Notes saved.")), self._error)

    def _export_history(self) -> None:
        row = self._selected()
        if row is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export research report", f"research-{row['id']}.md",
                                              "Markdown (*.md)")
        if not path:
            return
        session, history = self._c.session, self._core.research_history

        def work():
            record = history.get(session, row["id"])
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"# Research: {record['query']}\n\n_{record['created_at']}_\n\n{record['answer']}\n")
            return path

        self._c.run_async(work, lambda p: self._note(f"Exported to {p}."), self._error)

    def _delete_history(self) -> None:
        row = self._selected()
        if row is None:
            return
        session, history = self._c.session, self._core.research_history
        self._c.track(self.activity, "DELETING RECORD", lambda: history.delete(session, row["id"]),
                      lambda _r: (self.refresh(), self._note(f"Research #{row['id']} deleted.")), self._error)

    def _clear_history(self) -> None:
        if QMessageBox.question(self, "Clear research history", "Delete all of your research history? (Findings "
                                "saved to memory stay until you delete them.)") != QMessageBox.StandardButton.Yes:
            return
        session, history = self._c.session, self._core.research_history
        self._c.track(self.activity, "CLEARING HISTORY", lambda: history.clear(session, confirmed=True),
                      lambda n: (self.refresh(), self._note(f"{n} record(s) deleted.")), self._error)

    def _show_saved(self, rows: list[dict]) -> None:
        self._saved = rows
        self.saved_table.setRowCount(len(rows))
        for r, m in enumerate(rows):
            for c, value in enumerate([m["memory_key"], m["memory_value"], m["updated_at"][:16].replace("T", " ")]):
                self.saved_table.setItem(r, c, QTableWidgetItem(value))

    def _selected_saved(self) -> dict | None:
        rows = self.saved_table.selectionModel().selectedRows()
        if not rows:
            self._error("Select a saved finding first.")
            return None
        return self._saved[rows[0].row()]

    def _edit_saved(self) -> None:
        m = self._selected_saved()
        if m is None:
            return
        value, ok = QInputDialog.getMultiLineText(self, "Correct finding", m["memory_key"], m["memory_value"])
        if not ok or not value.strip():
            return
        session, memory = self._c.session, self._core.memory
        self._c.track(self.activity, "WRITING MEMORY", lambda: memory.update(session, m["id"], value=value.strip()),
                      lambda _r: (self.refresh(), self._note("Finding corrected.")), self._error)

    def _delete_saved(self) -> None:
        m = self._selected_saved()
        if m is None:
            return
        session, memory = self._c.session, self._core.memory
        self._c.track(self.activity, "ERASING MEMORY", lambda: memory.forget(session, m["id"]),
                      lambda _r: (self.refresh(), self._note("Finding deleted.")), self._error)

    def _show_settings(self, result) -> None:
        section, cfg = result
        self.web_provider.setCurrentText(section["web_provider"])
        self.news_provider.setCurrentText(section["news_provider"])
        self.keyless.setChecked(section["keyless_fallback"])
        self.auto_research.setChecked(section["auto_research"])
        self.robots.setChecked(section["respect_robots"])
        self.max_searches.setValue(section["max_searches"])
        self.max_pages.setValue(section["max_pages"])
        self.timeout.setValue(int(section["total_timeout_s"]))
        self.news_days.setValue(section["news_days"])
        self.region.setText(section["region"])
        keys = [f"Tavily: {'set' if cfg.tavily_key else 'not set'}", f"Brave: {'set' if cfg.brave_key else 'not set'}"]
        self.keys.setText(" · ".join(keys) + " — keys go in the .env file (see README 17.1); they are never shown here.")

    def _save_settings(self) -> None:
        changes = {"research": {"web_provider": self.web_provider.currentText(),
                                "news_provider": self.news_provider.currentText(),
                                "keyless_fallback": self.keyless.isChecked(), "auto_research": self.auto_research.isChecked(),
                                "respect_robots": self.robots.isChecked(), "max_searches": self.max_searches.value(),
                                "max_pages": self.max_pages.value(), "total_timeout_s": self.timeout.value(),
                                "news_days": self.news_days.value(), "region": self.region.text().strip().upper() or "IN"}}
        self._c.run_async(lambda: self._c.update_system_settings(changes),
                          lambda changed: self._note(f"Saved ({len(changed)} change(s))."), self._error)
