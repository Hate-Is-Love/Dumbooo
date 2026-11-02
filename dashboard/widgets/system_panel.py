"""System page: live CPU, RAM, battery and service status."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QProgressBar, QVBoxLayout, QWidget

from dashboard.dashboard_controller import DashboardController
from dashboard.styles import theme
from dashboard.widgets.hud import HudPanel, Sparkline, StatusDot, hud_label, hud_value
from services.system_monitor import SystemSnapshot, format_uptime

SERVICE_LABELS = {
    "microphone": "Microphone", "speaker": "Speaker", "wake_word": "Wake word", "vad": "Voice activity (VAD)",
    "stt": "Speech-to-text", "tts": "Text-to-speech", "ai": "AI provider", "reminders": "Reminders",
}


class MetricCard(HudPanel):
    def __init__(self, title: str, color: str = theme.CYAN) -> None:
        super().__init__(title)
        self.value = QLabel("—")
        self.value.setObjectName("BigValue")
        self.sub = hud_label("")
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.spark = Sparkline(color=color)
        self.body.addWidget(self.value)
        self.body.addWidget(self.sub)
        self.body.addWidget(self.bar)
        self.body.addWidget(self.spark)

    def update_metric(self, value: float | None, text: str, sub: str) -> None:
        self.value.setText(text)
        self.sub.setText(sub.upper())
        self.bar.setValue(int(value) if value is not None else 0)
        self.spark.add(value)


class SystemPanel(QWidget):
    def __init__(self, controller: DashboardController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._c = controller
        self._busy = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)
        cards = QHBoxLayout()
        self.cpu = MetricCard("CPU load")
        self.ram = MetricCard("Memory", theme.VIOLET)
        self.battery = MetricCard("Battery", theme.GREEN)
        for card in (self.cpu, self.ram, self.battery):
            cards.addWidget(card)
        layout.addLayout(cards)

        bottom = QHBoxLayout()
        info = HudPanel("Host")
        grid = QGridLayout()
        self.os_value = hud_value()
        self.uptime_value = hud_value()
        grid.addWidget(hud_label("Operating system"), 0, 0)
        grid.addWidget(self.os_value, 0, 1)
        grid.addWidget(hud_label("DUMBOO uptime"), 1, 0)
        grid.addWidget(self.uptime_value, 1, 1)
        info.body.addLayout(grid)
        info.body.addStretch(1)
        bottom.addWidget(info, 1)

        services = HudPanel("Services")
        self._service_rows: dict[str, tuple[StatusDot, QLabel]] = {}
        sgrid = QGridLayout()
        sgrid.setColumnStretch(2, 1)
        for row, (key, label) in enumerate(SERVICE_LABELS.items()):
            dot = StatusDot()
            detail = hud_value("…")
            detail.setWordWrap(True)
            sgrid.addWidget(dot, row, 0)
            sgrid.addWidget(hud_label(label), row, 1)
            sgrid.addWidget(detail, row, 2)
            self._service_rows[key] = (dot, detail)
        services.body.addLayout(sgrid)
        services.body.addStretch(1)
        bottom.addWidget(services, 2)
        layout.addLayout(bottom, 1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(2000)
        self.refresh()

    def flag_alert(self, alert: dict) -> None:
        """Module 4 resource alert: highlight the matching card's sub-label."""
        card = {"cpu": self.cpu, "ram": self.ram, "battery": self.battery}.get(alert.get("metric"))
        if card is not None:
            card.sub.setText(("ALERT: " + alert["message"]).upper()[:80])

    def refresh(self) -> None:
        if self._busy or self.window().isHidden():
            return  # don't sample while the dashboard sleeps
        self._busy = True
        self._c.run_async(self._c.core.monitor.sample, self._apply, lambda _m: setattr(self, "_busy", False))

    def _apply(self, snap: SystemSnapshot) -> None:
        self._busy = False
        if snap.cpu_percent is None:
            self.cpu.update_metric(None, "N/A", "unavailable")
        else:
            self.cpu.update_metric(snap.cpu_percent, f"{snap.cpu_percent:.0f}%", "all cores")
        if snap.ram_percent is None:
            self.ram.update_metric(None, "N/A", "unavailable")
        else:
            self.ram.update_metric(snap.ram_percent, f"{snap.ram_percent:.0f}%", f"{snap.ram_used_gb:.1f} / {snap.ram_total_gb:.1f} GB")
        if snap.battery_percent is None:
            self.battery.update_metric(None, "N/A", "no battery detected / unavailable")
        else:
            state = {True: "charging", False: "on battery", None: "charging state unavailable"}[snap.battery_charging]
            self.battery.update_metric(snap.battery_percent, f"{snap.battery_percent:.0f}%", state)
        self.os_value.setText(snap.os_name)
        self.uptime_value.setText(format_uptime(snap.uptime_s))
        for key, (dot, detail) in self._service_rows.items():
            status = snap.services.get(key)
            if status is None:
                dot.set_color(theme.MUTED)
                detail.setText("unknown")
                continue
            dot.set_color(theme.GREEN if status["ok"] else theme.RED)
            detail.setText(status["detail"])
