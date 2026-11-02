"""Dashboard-side management of automation (AUTOMATION page), with the checks done here, not in widgets.

* Viewing approved apps, permitted folders and settings: any signed-in user.
* Changing them: the owner-only ``automation.admin`` permission **and** a
  recent password confirmation (re-authentication), like other owner actions.
* Automation memories: the signed-in user's own, through ``MemoryService``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from automation.config import AutomationSettings, ConfigError, expand_path
from services.auth_service import AuthService, Session
from services.memory_service import MemoryService

AUTOMATION_KEYS = re.compile(r"(project path|folder|website|application)$|^workflow ")
SETTING_RANGES = {
    ("monitor", "cpu_alert_percent"): (50, 100), ("monitor", "ram_alert_percent"): (50, 100),
    ("monitor", "disk_alert_free_percent"): (1, 50), ("monitor", "battery_alert_percent"): (5, 50),
    ("monitor", "sustain_s"): (10, 3600), ("limits", "reply_wait_s"): (1, 60),
    ("limits", "confirmation_timeout_s"): (30, 3600),
}


class AutomationAdmin:
    def __init__(self, auth: AuthService, settings: AutomationSettings, memory: MemoryService) -> None:
        self._auth = auth
        self._settings = settings
        self._memory = memory

    def _owner(self, session: Session) -> None:
        self._auth._require_recent(session, "automation.admin")  # raises PermissionDenied / ReauthRequired

    # -- applications ----------------------------------------------------------------------------
    def applications(self, session: Session) -> list[dict]:
        self._auth.require(session)
        rows = []
        for app in sorted(self._settings.config.applications.values(), key=lambda a: a.name.lower()):
            exe = app.resolve()
            rows.append({"key": app.key, "name": app.name, "kind": app.kind, "installed": bool(exe or app.uri),
                         "path": str(exe) if exe else (app.uri or app.paths[0] if app.paths else ""),
                         "aliases": ", ".join(app.aliases), "custom": app.custom})
        return rows

    def add_application(self, session: Session, name: str, path: str, kind: str) -> dict:
        self._owner(session)
        exe = Path(path)
        if not exe.is_file():
            raise ConfigError(f"{path} does not exist.")
        key = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")[:40] or "app"
        app = self._settings.add_application(key, {"name": name.strip(), "aliases": [name.strip().lower()],
                                                   "paths": [str(exe)], "process": exe.name, "kind": kind,
                                                   "opens": ["file", "folder"] if kind == "editor" else ["file"]})
        return {"key": app.key, "name": app.name}

    def remove_application(self, session: Session, key: str) -> None:
        self._owner(session)
        if key not in self._settings.config.applications:
            raise ConfigError("That application is not in the list.")
        self._settings.remove_application(key)

    # -- permitted folders ----------------------------------------------------------------------------
    def folders(self, session: Session) -> list[dict]:
        self._auth.require(session)
        config = self._settings.config
        return [{"path": e["path"], "resolved": e["resolved"], "write": e["write"],
                 "exists": Path(e["resolved"]).is_dir()} for e in self._settings.permitted_entries()]

    def set_folders(self, session: Session, entries: list[dict]) -> None:
        self._owner(session)
        config = self._settings.config
        for entry in entries:
            path = expand_path(str(entry["path"]), self._settings.app_root, Path("."))
            if config.is_protected(path.resolve()):
                raise ConfigError(f"{path} is a protected location and cannot be permitted.")
        self._settings.set_permitted_directories(entries)

    # -- settings ----------------------------------------------------------------------------------------
    def settings(self, session: Session) -> dict:
        self._auth.require(session)
        config = self._settings.config
        return {"monitor": dict(config.monitor), "screen": dict(config.screen), "limits": dict(config.limits)}

    def update_settings(self, session: Session, section: str, values: dict) -> None:
        self._owner(session)
        clean: dict = {}
        for key, value in values.items():
            if section == "screen" and key == "ai_vision":
                if value not in ("ask", "allow", "never"):
                    raise ConfigError("Screenshot setting must be ask, allow or never.")
            elif section == "monitor" and key == "alerts_enabled":
                value = bool(value)
            else:
                lo, hi = SETTING_RANGES.get((section, key), (None, None))
                if lo is None or not isinstance(value, (int, float)) or not lo <= value <= hi:
                    raise ConfigError(f"{section}.{key} must be between {lo} and {hi}.")
            clean[key] = value
        self._settings.update_section(section, clean)

    # -- the user's automation memories --------------------------------------------------------------------
    def memories(self, session: Session) -> list[dict]:
        rows = []
        for m in self._memory.list(session):
            if not AUTOMATION_KEYS.search(m["key_norm"]):
                continue
            value = m["memory_value"]
            state = "—"
            if m["key_norm"].endswith(("project path", "folder")):
                state = "folder exists" if Path(value).is_dir() else "MISSING"
            elif m["key_norm"].endswith("website"):
                state = "web address"
            elif m["key_norm"].endswith("application"):
                state = "approved app" if any(value.lower() in a.names() for a in
                                              self._settings.config.applications.values()) else "NOT APPROVED"
            rows.append({"id": m["id"], "key": m["memory_key"], "value": value, "category": m["category"],
                         "updated_at": m["updated_at"], "state": state})
        return rows

    def export_config(self, session: Session) -> str:
        self._auth.require(session)
        config = self._settings.config
        return json.dumps({"applications": {k: a.as_dict() for k, a in config.applications.items()},
                           "permitted_directories": self._settings.permitted_entries()}, indent=2)
