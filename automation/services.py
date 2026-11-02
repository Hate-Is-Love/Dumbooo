"""The set of services automation capabilities may use (one object, passed to every handler)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from automation.config import AutomationConfig, AutomationSettings
from automation.safety import PathGuard

if TYPE_CHECKING:
    from app.event_bus import EventBus
    from automation.platform.browser import BrowserService
    from automation.platform.desktop import Desktop
    from automation.platform.processes import ProcessRunner
    from services.auth_service import AuthService
    from services.automation_history import AutomationHistory
    from services.memory_service import MemoryService
    from services.system_monitor import SystemMonitor


@dataclass
class AutomationServices:
    settings: AutomationSettings
    desktop: "Desktop"
    processes: "ProcessRunner"
    monitor: "SystemMonitor"
    browser: "BrowserService"
    memory: "MemoryService"
    history: "AutomationHistory"
    auth: "AuthService"
    bus: "EventBus"
    app_root: Path
    backup_dir: Path                    # inside DUMBOO's protected data folder (automation cannot touch it)
    # AI helper: generate(system_prompt, text, images=()) -> str; raises ai.errors.AIError
    ai_generate: Callable[..., str] | None = None
    uia: Any = None                     # automation.platform.uia (module) or a fake
    capture: Callable[..., Any] | None = None   # automation.platform.screen.capture or a fake
    probe: Callable[[str], tuple] | None = None  # web reachability check (tests replace it; None = real check)
    sleep: Callable[[float], None] = field(default=lambda s: __import__("time").sleep(s))

    @property
    def config(self) -> AutomationConfig:
        return self.settings.config

    @property
    def guard(self) -> PathGuard:
        return PathGuard(lambda: self.settings.config)

    def limit(self, name: str) -> int:
        return int(self.settings.config.limits[name])
