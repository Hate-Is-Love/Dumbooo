"""Automation configuration: approved applications, permitted folders, limits and risk policy.

Defaults ship in ``config/automation.json``. Changes made on the dashboard
(applications, permitted folders) are stored in ``data/automation.json``.
Everything is validated on load; invalid entries are skipped with a warning
instead of crashing DUMBOO.

Paths may use placeholders: ``{desktop}``, ``{documents}``, ``{downloads}``,
``{pictures}``, ``{music}``, ``{videos}``, ``{home}``, ``{app_root}`` and
Windows environment variables such as ``%LOCALAPPDATA%``. Known folders are
resolved with the Windows API, so OneDrive-redirected Desktop/Documents work.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

KINDS = ("browser", "editor", "files", "utility", "office", "media", "communication", "other")
OPENS = ("url", "file", "folder")

# Programs that can run arbitrary code or change the system. They can never be
# registered as "applications", whatever the configuration says.
BLOCKED_EXECUTABLES = frozenset({
    "cmd.exe", "powershell.exe", "pwsh.exe", "powershell_ise.exe", "wscript.exe", "cscript.exe", "mshta.exe",
    "rundll32.exe", "regsvr32.exe", "reg.exe", "regedit.exe", "regedt32.exe", "python.exe", "pythonw.exe",
    "py.exe", "pyw.exe", "bash.exe", "sh.exe", "wsl.exe", "wslhost.exe", "certutil.exe", "bitsadmin.exe",
    "msiexec.exe", "schtasks.exe", "sc.exe", "net.exe", "net1.exe", "netsh.exe", "diskpart.exe", "format.com",
    "bcdedit.exe", "taskkill.exe", "installutil.exe", "msbuild.exe", "cmstp.exe", "wmic.exe", "conhost.exe",
    "wt.exe", "windowsterminal.exe", "node.exe", "java.exe", "javaw.exe", "perl.exe", "ruby.exe", "mmc.exe",
    "control.exe", "gpedit.msc", "secpol.msc", "takeown.exe", "icacls.exe", "cipher.exe", "vssadmin.exe",
    "wevtutil.exe", "shutdown.exe", "runas.exe", "explorer++.exe", "hh.exe", "forfiles.exe", "pcalua.exe",
})

DEFAULT_LIMITS = {
    "reply_wait_s": 8, "confirmation_timeout_s": 300, "max_steps": 15, "max_bulk_files": 500,
    "search_max_results": 40, "search_timeout_s": 20, "search_max_depth": 8, "content_search_max_kb": 2048,
    "read_max_kb": 1024, "type_max_chars": 5000, "command_timeout_s": 900, "step_timeout_s": 120,
}
LIMIT_RANGES = {
    "reply_wait_s": (1, 60), "confirmation_timeout_s": (30, 3600), "max_steps": (1, 40), "max_bulk_files": (1, 5000),
    "search_max_results": (1, 500), "search_timeout_s": (2, 300), "search_max_depth": (1, 20),
    "content_search_max_kb": (16, 51200), "read_max_kb": (16, 51200), "type_max_chars": (1, 50000),
    "command_timeout_s": (10, 7200), "step_timeout_s": (5, 3600),
}


class ConfigError(ValueError):
    pass


# ---------------------------------------------------------------- known folders
_KNOWN_FOLDER_GUIDS = {
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "music": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
    "videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
}
_known_cache: dict[str, Path] = {}


def known_folder(name: str) -> Path:
    """Real location of a user folder (follows OneDrive redirection on Windows)."""
    if name in _known_cache:
        return _known_cache[name]
    home = Path.home()
    path = home / name.capitalize()
    if sys.platform == "win32" and name in _KNOWN_FOLDER_GUIDS:
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                            ("Data4", ctypes.c_ubyte * 8)]

            guid = GUID()
            ctypes.oledll.ole32.CLSIDFromString(_KNOWN_FOLDER_GUIDS[name], ctypes.byref(guid))
            out = ctypes.c_wchar_p()
            ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
            if out.value:
                path = Path(out.value)
            ctypes.windll.ole32.CoTaskMemFree(out)
        except Exception:
            log.debug("Known folder %s not resolved; using %s", name, path, exc_info=True)
    _known_cache[name] = path
    return path


def expand_path(raw: str, app_root: Path, data_dir: Path | None = None) -> Path:
    def repl(match: re.Match) -> str:
        key = match.group(1).lower()
        if key == "app_root":
            return str(app_root)
        if key == "data_dir" and data_dir is not None:
            return str(data_dir)
        if key == "home":
            return str(Path.home())
        if key in _KNOWN_FOLDER_GUIDS:
            return str(known_folder(key))
        raise ConfigError(f"unknown placeholder {{{key}}}")

    text = re.sub(r"\{([a-z_]+)\}", repl, raw.strip())
    text = os.path.expandvars(os.path.expanduser(text))
    if "%" in text:
        raise ConfigError(f"unknown environment variable in {raw}")
    return Path(text)


# ---------------------------------------------------------------- data classes
@dataclass(frozen=True)
class AppEntry:
    key: str
    name: str
    aliases: tuple[str, ...]
    paths: tuple[str, ...]
    process: str
    kind: str
    opens: tuple[str, ...]
    uri: str = ""            # e.g. "ms-settings:" (launched through the shell instead of a path)
    custom: bool = False     # added on the dashboard (not part of the shipped defaults)

    def resolve(self) -> Path | None:
        """The first configured executable that exists on this computer."""
        for raw in self.paths:
            try:
                path = Path(os.path.expandvars(raw))
            except Exception:
                continue
            if path.is_file():
                return path
        return None

    def names(self) -> set[str]:
        return {self.key.lower(), self.name.lower(), *(a.lower() for a in self.aliases)}

    def as_dict(self) -> dict:
        return {"name": self.name, "aliases": list(self.aliases), "paths": list(self.paths), "process": self.process,
                "kind": self.kind, "opens": list(self.opens), **({"uri": self.uri} if self.uri else {})}


@dataclass(frozen=True)
class PermittedDir:
    raw: str
    path: Path
    write: bool


@dataclass
class AutomationConfig:
    applications: dict[str, AppEntry]
    permitted: list[PermittedDir]
    websites: dict[str, str]
    limits: dict[str, int]
    risk_overrides: dict[str, str]
    auto_approve_medium: list[str]
    browser: dict[str, Any]
    monitor: dict[str, Any]
    screen: dict[str, Any]
    protected: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Folders inside a protected folder that are *not* protected (only AppData\Local\Temp). They are still
    # only accessible when they are inside a permitted folder.
    protected_exempt: list[Path] = field(default_factory=list)

    def is_protected(self, path: Path) -> bool:
        """True for DUMBOO's own data, Windows, Program Files, ProgramData, AppData and DUMBOO's .venv."""
        for protected in self.protected:
            if not _inside(path, protected):
                continue
            if any(_inside(path, e) and _inside(e, protected) for e in self.protected_exempt):
                continue
            return True
        return False


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_app(key: str, raw: dict, custom: bool) -> AppEntry:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", key):
        raise ConfigError(f"application key '{key}' must be lowercase letters, digits, - or _")
    name = str(raw.get("name", "")).strip()
    if not 1 <= len(name) <= 60:
        raise ConfigError(f"application {key}: name must be 1-60 characters")
    paths = tuple(str(p) for p in raw.get("paths", []) if str(p).strip())
    uri = str(raw.get("uri", "")).strip()
    if not paths and not uri:
        raise ConfigError(f"application {key}: needs at least one path")
    for p in paths:
        exe = Path(os.path.expandvars(p)).name.lower()
        if not exe.endswith(".exe"):
            raise ConfigError(f"application {key}: only .exe programs can be registered")
        if exe in BLOCKED_EXECUTABLES:
            raise ConfigError(f"application {key}: {exe} can run arbitrary commands and cannot be registered")
    if uri and not re.fullmatch(r"ms-settings:[a-z-]*", uri):
        raise ConfigError(f"application {key}: only ms-settings: URIs are supported")
    process = str(raw.get("process") or (Path(os.path.expandvars(paths[0])).name if paths else "")).strip()
    if process.lower() in BLOCKED_EXECUTABLES:
        raise ConfigError(f"application {key}: blocked process name")
    kind = str(raw.get("kind", "other"))
    if kind not in KINDS:
        raise ConfigError(f"application {key}: kind must be one of {', '.join(KINDS)}")
    opens = tuple(o for o in raw.get("opens", []) if o in OPENS)
    aliases = tuple(" ".join(str(a).lower().split()) for a in raw.get("aliases", []) if str(a).strip())[:12]
    return AppEntry(key, name, aliases, paths, process, kind, opens, uri, custom)


def _merge(defaults: dict, user: dict) -> dict:
    merged = copy.deepcopy(defaults)
    for section in ("limits", "browser", "monitor", "screen", "websites", "risk_overrides"):
        if isinstance(user.get(section), dict):
            merged.setdefault(section, {}).update(user[section])
    if isinstance(user.get("auto_approve_medium"), list):
        merged["auto_approve_medium"] = user["auto_approve_medium"]
    if isinstance(user.get("permitted_directories"), list):
        merged["permitted_directories"] = user["permitted_directories"]
    apps = merged.setdefault("applications", {})
    for key, value in (user.get("applications") or {}).items():
        if value is None or (isinstance(value, dict) and value.get("removed")):
            apps.pop(key, None)
        elif isinstance(value, dict):
            apps[key] = dict(value, _custom=True)
    return merged


def build_config(raw: dict, app_root: Path, data_dir: Path) -> AutomationConfig:
    warnings: list[str] = []
    apps: dict[str, AppEntry] = {}
    for key, value in (raw.get("applications") or {}).items():
        try:
            apps[key] = _validate_app(key, value, bool(value.get("_custom")))
        except (ConfigError, AttributeError, TypeError) as exc:
            warnings.append(str(exc))
    permitted: list[PermittedDir] = []
    for entry in raw.get("permitted_directories") or []:
        try:
            path = expand_path(str(entry["path"]), app_root, data_dir).resolve()
            if path.anchor == str(path):
                raise ConfigError(f"a whole drive ({path}) cannot be a permitted folder")
            permitted.append(PermittedDir(str(entry["path"]), path, bool(entry.get("write", False))))
        except (ConfigError, KeyError, TypeError, OSError) as exc:
            warnings.append(f"permitted folder skipped: {exc}")
    websites = {" ".join(k.lower().split()): str(v) for k, v in (raw.get("websites") or {}).items()
                if str(v).startswith("https://") or str(v).startswith("http://")}
    limits = dict(DEFAULT_LIMITS)
    for key, value in (raw.get("limits") or {}).items():
        lo_hi = LIMIT_RANGES.get(key)
        if lo_hi and isinstance(value, (int, float)) and not isinstance(value, bool) and lo_hi[0] <= value <= lo_hi[1]:
            limits[key] = int(value)
        else:
            warnings.append(f"limit {key} ignored")
    risks = {k: v for k, v in (raw.get("risk_overrides") or {}).items() if v in ("low", "medium", "high")}
    auto = [str(c) for c in raw.get("auto_approve_medium") or []]
    browser = dict(raw.get("browser") or {})
    browser.setdefault("channel", "msedge")
    if browser.get("channel") not in ("msedge", "chrome", "chromium"):
        warnings.append("browser.channel must be msedge, chrome or chromium; using msedge")
        browser["channel"] = "msedge"
    try:
        browser["download_path"] = expand_path(str(browser.get("download_dir") or "{downloads}\\DUMBOO"),
                                               app_root, data_dir)
    except ConfigError as exc:
        warnings.append(str(exc))
        browser["download_path"] = known_folder("downloads") / "DUMBOO"
    if "{query}" not in str(browser.get("search_engine", "")):
        browser["search_engine"] = "https://www.bing.com/search?q={query}"
    monitor = dict(raw.get("monitor") or {})
    screen = dict(raw.get("screen") or {})
    if screen.get("ai_vision") not in ("ask", "allow", "never"):
        screen["ai_vision"] = "ask"
    home = Path.home()
    protected = [_resolved(p) for p in (
        data_dir, home / "AppData", Path(os.environ.get("WINDIR", r"C:\Windows")),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        Path(os.environ.get("ProgramData", r"C:\ProgramData")), app_root / ".venv")]
    exempt = [_resolved(home / "AppData" / "Local" / "Temp")]
    for w in warnings:
        log.warning("Automation config: %s", w)
    return AutomationConfig(apps, permitted, websites, limits, risks, auto, browser, monitor, screen,
                            protected, warnings, exempt)


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


class AutomationSettings:
    """Loads, validates and saves the automation configuration (thread-safe)."""

    def __init__(self, defaults_path: Path, user_path: Path, app_root: Path, data_dir: Path) -> None:
        self._defaults_path = defaults_path
        self._user_path = user_path
        self._app_root = app_root
        self._data_dir = data_dir
        self._lock = threading.RLock()
        self._defaults = json.loads(defaults_path.read_text(encoding="utf-8"))
        self._user = self._read_user()
        self._config = build_config(_merge(self._defaults, self._user), app_root, data_dir)

    def _read_user(self) -> dict:
        if not self._user_path.exists():
            return {}
        try:
            data = json.loads(self._user_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Ignoring unreadable %s: %s", self._user_path.name, exc)
            return {}

    @property
    def config(self) -> AutomationConfig:
        with self._lock:
            return self._config

    @property
    def app_root(self) -> Path:
        return self._app_root

    def _save(self, user: dict) -> AutomationConfig:
        config = build_config(_merge(self._defaults, user), self._app_root, self._data_dir)
        tmp = self._user_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(user, indent=2), encoding="utf-8")
        os.replace(tmp, self._user_path)
        with self._lock:
            self._user = user
            self._config = config
        return config

    # -- changes made on the dashboard (callers check the owner permission first) --------
    def add_application(self, key: str, entry: dict) -> AppEntry:
        app = _validate_app(key, entry, True)  # raises ConfigError with a readable reason
        with self._lock:
            user = copy.deepcopy(self._user)
        user.setdefault("applications", {})[key] = app.as_dict()
        self._save(user)
        return self.config.applications[key]

    def remove_application(self, key: str) -> None:
        with self._lock:
            user = copy.deepcopy(self._user)
        user.setdefault("applications", {})[key] = {"removed": True}
        self._save(user)

    def set_permitted_directories(self, entries: list[dict]) -> AutomationConfig:
        for entry in entries:
            path = expand_path(str(entry["path"]), self._app_root, self._data_dir)
            if not path.is_absolute():
                raise ConfigError(f"{entry['path']} is not a full path")
            if path.resolve().anchor == str(path.resolve()):
                raise ConfigError("a whole drive cannot be a permitted folder")
        with self._lock:
            user = copy.deepcopy(self._user)
        user["permitted_directories"] = [{"path": str(e["path"]), "write": bool(e.get("write"))} for e in entries]
        return self._save(user)

    def update_section(self, section: str, values: dict) -> AutomationConfig:
        if section not in ("limits", "monitor", "screen", "browser"):
            raise ConfigError(f"unknown section {section}")
        with self._lock:
            user = copy.deepcopy(self._user)
        user.setdefault(section, {}).update(values)
        return self._save(user)

    def permitted_entries(self) -> list[dict]:
        return [{"path": p.raw, "resolved": str(p.path), "write": p.write} for p in self.config.permitted]
