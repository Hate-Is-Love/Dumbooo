"""SECURITY page (Module 7): real results from GuardianService, with honest loading/error states.

Every check runs in the background (the UI stays responsive) and shows exactly what was collected.
Disabling a startup entry asks for confirmation in a dialog that explains the change; it can be
restored from the same table.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QTabWidget,
                               QTextBrowser, QVBoxLayout, QWidget)

from dashboard.styles import theme
from dashboard.widgets.activity import ScanIndicator
from dashboard.widgets.conversation_panel import markdown_html
from guardian import report

STATUS_COLOR = {"PASS": theme.GREEN, "WARNING": theme.AMBER, "FAIL": theme.RED, "UNKNOWN": theme.MUTED,
                "NOT_SUPPORTED": theme.MUTED}


def _browser() -> QTextBrowser:
    b = QTextBrowser()
    b.setOpenExternalLinks(True)
    return b


def _table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.horizontalHeader().setStretchLastSection(True)
    t.verticalHeader().setVisible(False)
    return t


class SecurityPanel(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self._c = controller
        self._entries = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        top = QHBoxLayout()
        self.activity = ScanIndicator("SECURITY GUARDIAN READY")
        top.addWidget(self.activity, 1)
        self.stop = QPushButton("■  STOP SCAN")
        self.stop.clicked.connect(lambda: self._svc().cancel())
        top.addWidget(self.stop)
        layout.addLayout(top)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self._build_overview()
        self._build_startup()
        self._build_processes()
        self._build_simple("NETWORK", "SHOW CONNECTIONS", self._network)
        self._build_simple("UPDATES", "CHECK UPDATES && ADVISORIES", self._updates)
        self._build_link()
        self._build_integrity()
        self._build_history()
        self._build_settings()

    def _svc(self):
        return self._c.core.guardian

    def _session(self):
        return self._c.session

    def _note(self, text: str, ok: bool = True) -> None:
        self.status.setStyleSheet(f"color: {theme.GREEN if ok else theme.RED};")
        self.status.setText(text)

    def _run(self, label: str, work, done) -> None:
        self.status.setText("")
        self._c.track(self.activity, label, work, done, lambda msg: self._note(msg, ok=False), done_label="DONE")

    def _show(self, browser: QTextBrowser, markdown: str) -> None:
        browser.setHtml(markdown_html(markdown))

    # -- overview ---------------------------------------------------------------------------------------------
    def _build_overview(self) -> None:
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        run = QPushButton("◉  RUN SECURITY CHECK")
        run.setObjectName("Primary")
        run.clicked.connect(self._overview)
        self.last_scan = QLabel("No check yet in this session.")
        self.last_scan.setObjectName("HudLabel")
        row.addWidget(run)
        row.addWidget(self.last_scan, 1)
        v.addLayout(row)
        self.checks = _table(["Status", "Check", "Result", "Source"])
        self.checks.setColumnWidth(0, 120)
        self.checks.setColumnWidth(1, 240)
        self.checks.setColumnWidth(2, 520)
        v.addWidget(self.checks, 1)
        self.overview_note = QLabel("UNKNOWN means DUMBOO couldn't read that setting (often it needs administrator "
                                    "rights) — not that it is off. Nothing is changed by a check.")
        self.overview_note.setWordWrap(True)
        self.overview_note.setObjectName("HudLabel")
        v.addWidget(self.overview_note)
        self.tabs.addTab(w, "OVERVIEW")

    def _overview(self) -> None:
        session = self._session()
        self._run("CHECKING SECURITY", lambda: self._svc().overview(session), self._show_overview)

    def _show_overview(self, result: dict) -> None:
        self.checks.setRowCount(0)
        for c in result["checks"]:
            r = self.checks.rowCount()
            self.checks.insertRow(r)
            status = QTableWidgetItem(c["status"])
            status.setForeground(theme.qcolor(STATUS_COLOR.get(c["status"], theme.TEXT)))
            self.checks.setItem(r, 0, status)
            self.checks.setItem(r, 1, QTableWidgetItem(c["title"]))
            detail = c["detail"] + (f" — {c['advice']}" if c.get("advice") else "")
            item = QTableWidgetItem(detail)
            item.setToolTip(detail)
            self.checks.setItem(r, 2, item)
            self.checks.setItem(r, 3, QTableWidgetItem(c.get("source", "")))
        counts = ", ".join(f"{n} {k}" for k, n in result["counts"].items() if n)
        self.last_scan.setText(f"Last check {result['time'].replace('T', ' ')} · {counts}")

    # -- startup ----------------------------------------------------------------------------------------------
    def _build_startup(self) -> None:
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        for text, fn, primary in (("LIST STARTUP ENTRIES", self._startup, True), ("INVESTIGATE", self._investigate, False),
                                  ("DISABLE…", self._disable, False), ("RESTORE…", self._restore, False),
                                  ("SAVE REPORT", self._save_report, False)):
            b = QPushButton(text)
            if primary:
                b.setObjectName("Primary")
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self.startup_table = _table(["", "Name", "Type", "Applies to", "Publisher", "Notes"])
        for col, width in ((0, 30), (1, 220), (2, 120), (3, 90), (4, 220)):
            self.startup_table.setColumnWidth(col, width)
        self.startup_table.doubleClicked.connect(lambda _i: self._investigate())
        v.addWidget(self.startup_table, 2)
        self.startup_report = _browser()
        v.addWidget(self.startup_report, 3)
        self.tabs.addTab(w, "STARTUP")

    def _startup(self) -> None:
        session = self._session()
        self._run("READING STARTUP ENTRIES", lambda: self._svc().startup(session), self._show_startup)

    def _show_startup(self, entries) -> None:
        mech = {"registry_run": "Run key", "startup_folder": "Startup folder", "scheduled_task": "Task",
                "service": "Service"}
        self._entries = sorted(entries, key=lambda e: (not e.flags, e.mechanism, e.name.lower()))
        self.startup_table.setRowCount(0)
        for e in self._entries:
            r = self.startup_table.rowCount()
            self.startup_table.insertRow(r)
            mark = QTableWidgetItem("⚠" if e.flags else "✓")
            mark.setForeground(theme.qcolor(theme.AMBER if e.flags else theme.GREEN))
            self.startup_table.setItem(r, 0, mark)
            for col, text in enumerate((e.name, mech.get(e.mechanism, e.mechanism),
                                        "all users" if e.scope == "machine" else "you",
                                        e.signature.publisher if e.signature else "UNKNOWN", "; ".join(e.flags)), 1):
                item = QTableWidgetItem(text)
                item.setToolTip(f"{e.command}\n{e.location}")
                self.startup_table.setItem(r, col, item)
        flagged = sum(1 for e in entries if e.flags)
        self._note(f"{len(entries)} startup entries · {flagged} with something to look at. Double-click one to "
                   "investigate it.")

    def _selected(self):
        row = self.startup_table.currentRow()
        if row < 0 or row >= len(self._entries):
            self._note("Select a startup entry first.", ok=False)
            return None
        return self._entries[row]

    def _investigate(self) -> None:
        entry = self._selected()
        if entry is None:
            return
        session = self._session()
        self._run(f"INVESTIGATING {entry.name.upper()[:30]}", lambda: self._svc().investigate(session, entry),
                  lambda inv: self._show(self.startup_report, report.investigation_markdown(inv)))

    def _disable(self) -> None:
        entry = self._selected()
        if entry is None:
            return
        session = self._session()
        try:
            proposal = self._svc().propose_disable(session, entry)
        except Exception as exc:
            self._note(getattr(exc, "user_message", str(exc)), ok=False)
            return
        if not proposal["supported"]:
            self._note(proposal["message"], ok=False)
            return
        answer = QMessageBox.question(self, "Disable startup entry", proposal["message"].replace(
            "Say “yes” to disable it or “no” to keep it.", "Disable it now?"))
        if answer != QMessageBox.StandardButton.Yes:
            self._svc().decline(session, proposal["action_id"])
            self._note(f"Kept “{entry.name}”.")
            return
        self._run("DISABLING", lambda: self._svc().confirm_disable(session, proposal["action_id"], entry),
                  lambda msg: (self._note(msg), self._startup()))

    def _restore(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        name, ok = QInputDialog.getText(self, "Restore startup entry", "Name of the startup entry to restore:")
        if not ok or not name.strip():
            return
        session = self._session()
        self._run("RESTORING", lambda: self._svc().restore(session, name.strip()),
                  lambda msg: (self._note(msg), self._startup()))

    def _save_report(self) -> None:
        session = self._session()
        inv = self._svc().last_investigation(session)
        if inv is None:
            self._note("Investigate an entry first.", ok=False)
            return
        self._run("SAVING REPORT", lambda: self._svc().save_report(session, report.investigation_markdown(inv),
                                                                    inv.subject),
                  lambda path: self._note(f"Report saved to {path}"))

    # -- processes ------------------------------------------------------------------------------------------------
    def _build_processes(self) -> None:
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        self.sort = QComboBox()
        self.sort.addItems(["memory", "cpu"])
        listing = QPushButton("LIST PROCESSES")
        listing.setObjectName("Primary")
        listing.clicked.connect(self._processes)
        self.proc_name = QLineEdit()
        self.proc_name.setPlaceholderText("process name or PID, e.g. chrome or 1234")
        self.proc_name.returnPressed.connect(self._inspect)
        inspect = QPushButton("INSPECT")
        inspect.clicked.connect(self._inspect)
        row.addWidget(QLabel("Sort by"))
        row.addWidget(self.sort)
        row.addWidget(listing)
        row.addSpacing(20)
        row.addWidget(self.proc_name, 1)
        row.addWidget(inspect)
        v.addLayout(row)
        self.proc_table = _table(["PID", "Program", "Memory (MB)", "CPU %", "User", "Path"])
        self.proc_table.doubleClicked.connect(self._inspect_row)
        v.addWidget(self.proc_table, 2)
        self.proc_report = _browser()
        v.addWidget(self.proc_report, 2)
        note = QLabel("High CPU or memory use is not a sign of malware by itself. Processes are never ended from here.")
        note.setObjectName("HudLabel")
        v.addWidget(note)
        self.tabs.addTab(w, "PROCESSES")

    def _processes(self) -> None:
        session, sort = self._session(), self.sort.currentText()
        self._run("READING PROCESSES", lambda: self._svc().processes(session, sort), self._show_processes)

    def _show_processes(self, rows) -> None:
        self.proc_table.setRowCount(0)
        for p in rows:
            r = self.proc_table.rowCount()
            self.proc_table.insertRow(r)
            for col, value in enumerate((p["pid"], p["name"], p["memory_mb"], p["cpu"], p["user"], p["exe"])):
                self.proc_table.setItem(r, col, QTableWidgetItem(str(value)))

    def _inspect_row(self, index) -> None:
        item = self.proc_table.item(index.row(), 0)
        if item is not None:
            self.proc_name.setText(item.text())
            self._inspect()

    def _inspect(self) -> None:
        target = self.proc_name.text().strip()
        if not target:
            self._note("Type a process name or PID.", ok=False)
            return
        session = self._session()

        def done(inv) -> None:
            if inv is None:
                self._note(f"No running process called “{target}”.", ok=False)
            else:
                self._show(self.proc_report, report.investigation_markdown(inv).replace("**Investigation:", "**Process:"))
        self._run("INSPECTING PROCESS", lambda: self._svc().inspect_process(session, target), done)

    # -- simple report tabs ----------------------------------------------------------------------------------------
    def _build_simple(self, title: str, button: str, fn) -> None:
        w = QWidget()
        v = QVBoxLayout(w)
        b = QPushButton(button)
        b.setObjectName("Primary")
        b.clicked.connect(fn)
        row = QHBoxLayout()
        row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        browser = _browser()
        setattr(self, f"{title.lower()}_report", browser)
        v.addWidget(browser, 1)
        self.tabs.addTab(w, title)

    def _network(self) -> None:
        session = self._session()
        self._run("READING CONNECTIONS", lambda: self._svc().network(session),
                  lambda n: self._show(self.network_report, report.network_markdown(n)))

    def _updates(self) -> None:
        session = self._session()
        self._run("CHECKING UPDATES", lambda: self._svc().updates(session),
                  lambda u: self._show(self.updates_report, report.updates_markdown(u)))

    # -- link check ------------------------------------------------------------------------------------------------
    def _build_link(self) -> None:
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("paste a link, e.g. https://example.com/login")
        self.url.returnPressed.connect(lambda: self._link(False))
        check = QPushButton("CHECK LINK")
        check.setObjectName("Primary")
        check.clicked.connect(lambda: self._link(False))
        trace = QPushButton("TRACE REDIRECTS")
        trace.setToolTip("Follows the link's redirects safely: public addresses only, no scripts, no downloads.")
        trace.clicked.connect(lambda: self._link(True))
        row.addWidget(self.url, 1)
        row.addWidget(check)
        row.addWidget(trace)
        v.addLayout(row)
        self.link_report = _browser()
        v.addWidget(self.link_report, 1)
        note = QLabel("Links are never opened in your browser. Email text pasted in the chat (“is this email a scam: "
                      "…”) is analysed on this PC only.")
        note.setWordWrap(True)
        note.setObjectName("HudLabel")
        v.addWidget(note)
        self.tabs.addTab(w, "LINK CHECK")

    def _link(self, trace: bool) -> None:
        url = self.url.text().strip()
        if not url:
            self._note("Paste a link first.", ok=False)
            return
        session = self._session()
        self._run("CHECKING LINK", lambda: self._svc().check_url(session, url, trace),
                  lambda r: self._show(self.link_report, report.url_markdown(r)))

    # -- integrity -------------------------------------------------------------------------------------------------
    def _build_integrity(self) -> None:
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        self.scope_path = QLineEdit()
        self.scope_path.setPlaceholderText("folder or file to monitor")
        browse = QPushButton("BROWSE…")
        browse.clicked.connect(self._browse)
        add = QPushButton("MONITOR (BASELINE)")
        add.setObjectName("Primary")
        add.clicked.connect(self._add_scope)
        row.addWidget(self.scope_path, 1)
        row.addWidget(browse)
        row.addWidget(add)
        v.addLayout(row)
        self.scope_table = _table(["Name", "Files", "Baseline", "Last check", "Path"])
        v.addWidget(self.scope_table, 1)
        row2 = QHBoxLayout()
        check = QPushButton("CHECK SELECTED")
        check.clicked.connect(self._check_scope)
        rebase = QPushButton("RE-BASELINE SELECTED")
        rebase.clicked.connect(self._rebaseline)
        refresh = QPushButton("REFRESH")
        refresh.clicked.connect(self._load_scopes)
        for b in (check, rebase, refresh):
            row2.addWidget(b)
        row2.addStretch(1)
        v.addLayout(row2)
        self.integrity_report = _browser()
        v.addWidget(self.integrity_report, 2)
        self.tabs.addTab(w, "INTEGRITY")

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Folder to monitor")
        if folder:
            self.scope_path.setText(folder)

    def _add_scope(self) -> None:
        path = self.scope_path.text().strip()
        if not path:
            self._note("Choose a folder first.", ok=False)
            return
        session = self._session()
        self._run("CREATING BASELINE", lambda: self._svc().add_scope(session, path),
                  lambda r: (self._note(f"Monitoring {r['scope']}: baseline of {r['files']} files."), self._load_scopes()))

    def _load_scopes(self) -> None:
        session = self._session()
        self._run("LOADING", lambda: self._svc().scopes(session), self._show_scopes)

    def _show_scopes(self, rows) -> None:
        self._scopes = rows
        self.scope_table.setRowCount(0)
        for s in rows:
            r = self.scope_table.rowCount()
            self.scope_table.insertRow(r)
            for col, value in enumerate((s["label"], s["files"], (s["baseline_at"] or "")[:16].replace("T", " "),
                                         (s["checked_at"] or "never")[:16].replace("T", " "), s["path"])):
                self.scope_table.setItem(r, col, QTableWidgetItem(str(value)))

    def _scope_selected(self) -> str | None:
        row = self.scope_table.currentRow()
        if row < 0 or row >= len(getattr(self, "_scopes", [])):
            self._note("Select a monitored folder first.", ok=False)
            return None
        return self._scopes[row]["path"]

    def _check_scope(self) -> None:
        path = self._scope_selected()
        if path is None:
            return
        session = self._session()

        def done(r) -> None:
            c = r["counts"]
            lines = [f"**{r['scope']}** — {c['modified']} modified, {c['missing']} missing, {c['new']} new "
                     f"({r['files']} files checked)", ""]
            lines += [f"- **{e['kind']}** `{e['rel_path']}`" for e in r["events"][:200]] or ["No changes since the baseline."]
            lines += ["", "_A change is an observation, not proof of an attack. Nothing was changed._"]
            self._show(self.integrity_report, "\n".join(lines))
            self._load_scopes()
        self._run("CHECKING INTEGRITY", lambda: self._svc().check_integrity(session, path), done)

    def _rebaseline(self) -> None:
        path = self._scope_selected()
        if path is None:
            return
        session = self._session()
        self._run("CREATING BASELINE", lambda: self._svc().baseline(session, path),
                  lambda r: (self._note(f"New baseline for {r['scope']}: {r['files']} files."), self._load_scopes()))

    # -- history ------------------------------------------------------------------------------------------------------
    def _build_history(self) -> None:
        w = QWidget()
        v = QVBoxLayout(w)
        b = QPushButton("REFRESH HISTORY")
        b.clicked.connect(self._history)
        row = QHBoxLayout()
        row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self.history_report = _browser()
        v.addWidget(self.history_report, 1)
        self.tabs.addTab(w, "HISTORY")

    def _history(self) -> None:
        session = self._session()

        def done(d) -> None:
            lines = ["**Scans**", "", "| When | Check | Status | Summary |", "|---|---|---|---|"]
            lines += [f"| {s['started_at'][:16].replace('T', ' ')} | {s['kind']} | {s['status']} | {s['summary']} |"
                      for s in d["scans"]]
            lines += ["", "**Findings**", "", "| When | Area | Subject | Result | Confidence |", "|---|---|---|---|---|"]
            lines += [f"| {f['created_at'][:16].replace('T', ' ')} | {f['category']} | {f['subject']} | "
                      f"{f['assessment'] or f['status']} | {f['confidence']} |" for f in d["findings"]]
            lines += ["", "**Actions (audit log)**", "", "| When | Action | Target | Status |", "|---|---|---|---|"]
            lines += [f"| {a['created_at'][:16].replace('T', ' ')} | {a['action']} (level {a['level']}) | "
                      f"{a['target']} | {a['status']} |" for a in d["actions"]]
            self._show(self.history_report, "\n".join(lines))
        self._run("LOADING HISTORY", lambda: self._svc().latest(session, 50), done)

    # -- settings -----------------------------------------------------------------------------------------------------
    def _build_settings(self) -> None:
        w = QWidget()
        f = QFormLayout(w)
        self.s_enabled = QCheckBox("Security Guardian on")
        self.s_external = QCheckBox("Allow external lookups (sends file hashes to the threat-intelligence provider)")
        self.s_provider = QComboBox()
        self.s_provider.addItems(["none", "virustotal", "malwarebazaar"])
        self.s_feeds = QCheckBox("Download public security advisory catalogues (CISA KEV; nothing about you is sent)")
        self.s_integrity = QCheckBox("File integrity monitoring")
        self.s_timeout = QComboBox()
        self.s_timeout.addItems(["60", "120", "300", "600"])
        save = QPushButton("SAVE")
        save.setObjectName("Primary")
        save.clicked.connect(self._save_settings)
        keys = QLabel("API keys go in .env: VIRUSTOTAL_API_KEY or MALWAREBAZAAR_API_KEY. Without a key, lookups stay "
                      "NOT_CHECKED. DUMBOO never enables a paid service or changes anything automatically.")
        keys.setWordWrap(True)
        keys.setObjectName("HudLabel")
        for widget in (self.s_enabled, self.s_external):
            f.addRow(widget)
        f.addRow("Threat intelligence", self.s_provider)
        f.addRow(self.s_feeds)
        f.addRow(self.s_integrity)
        f.addRow("Scan time limit (s)", self.s_timeout)
        row = QHBoxLayout()
        row.addWidget(save)
        row.addStretch(1)
        f.addRow(row)
        f.addRow(keys)
        self.tabs.addTab(w, "SETTINGS")
        self._load_settings()

    def _load_settings(self) -> None:
        s = self._c.core.settings.section("security")
        self.s_enabled.setChecked(s["monitor_enabled"])
        self.s_external.setChecked(s["external_lookups"])
        self.s_provider.setCurrentText(s["threat_intel_provider"])
        self.s_feeds.setChecked(s["advisory_feeds"])
        self.s_integrity.setChecked(s["file_integrity_enabled"])
        self.s_timeout.setCurrentText(str(int(s["scan_timeout_s"])))

    def _save_settings(self) -> None:
        changes = {"security": {"monitor_enabled": self.s_enabled.isChecked(),
                                "external_lookups": self.s_external.isChecked(),
                                "threat_intel_provider": self.s_provider.currentText(),
                                "advisory_feeds": self.s_feeds.isChecked(),
                                "file_integrity_enabled": self.s_integrity.isChecked(),
                                "scan_timeout_s": int(self.s_timeout.currentText())}}
        self._run("SAVING", lambda: self._c.update_system_settings(changes),
                  lambda changed: self._note(f"Saved {len(changed)} setting(s)." if changed else "No changes."))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._session() is not None and not getattr(self, "_scopes", None):
            self._load_scopes()


__all__ = ["SecurityPanel", "Qt"]
