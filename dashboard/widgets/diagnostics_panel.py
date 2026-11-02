"""Diagnostics page: versions, devices, model files, service status and recent events."""

from __future__ import annotations

import os
from datetime import datetime

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from dashboard.dashboard_controller import DashboardController
from dashboard.widgets.hud import HudPanel
from utils import diagnostics

# Events worth listing in the live log (high-rate level events are skipped).
_LOGGED = {"state.changed", "wake.detected", "wake.status", "mic.status", "speech.started", "speech.ended",
           "utterance.rejected", "listen.timeout", "stt.transcript", "stt.failed", "stt.status", "tts.status",
           "playback.started", "playback.finished", "interruption", "reminder.due", "settings.changed", "error"}


class DiagnosticsPanel(QWidget):
    def __init__(self, controller: DashboardController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._c = controller
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)

        report = HudPanel("System report")
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        report.body.addWidget(self.report, 1)
        row = QHBoxLayout()
        refresh = QPushButton("RUN DIAGNOSTICS")
        refresh.setObjectName("Primary")
        refresh.clicked.connect(self.refresh)
        copy = QPushButton("COPY REPORT")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.report.toPlainText()))
        logs = QPushButton("OPEN LOG FOLDER")
        logs.clicked.connect(self._open_logs)
        row.addWidget(refresh)
        row.addWidget(copy)
        row.addWidget(logs)
        row.addStretch(1)
        report.body.addLayout(row)
        layout.addWidget(report, 3)

        events = HudPanel("Event log // live")
        self.event_view = QPlainTextEdit()
        self.event_view.setReadOnly(True)
        self.event_view.setMaximumBlockCount(300)
        events.body.addWidget(self.event_view, 1)
        layout.addWidget(events, 2)
        controller.bridge.event.connect(self._on_event)

    def refresh(self) -> None:
        core = self._c.core
        self.report.setPlainText("Collecting…")
        self._c.run_async(
            lambda: diagnostics.format_report(diagnostics.collect(core.paths, core.settings.snapshot(), core.service_status())),
            self.report.setPlainText, lambda m: self.report.setPlainText(f"Diagnostics failed: {m}"))

    def _on_event(self, name: str, payload: dict) -> None:
        if name not in _LOGGED:
            return
        detail = ", ".join(f"{k}={v}" for k, v in payload.items() if k not in ("settings",))
        self.event_view.appendPlainText(f"{datetime.now():%H:%M:%S}  {name:<20} {detail[:160]}")

    def _open_logs(self) -> None:
        path = self._c.core.paths.logs_dir
        if hasattr(os, "startfile"):
            os.startfile(path)  # noqa: S606 - opens the log folder in Explorer
