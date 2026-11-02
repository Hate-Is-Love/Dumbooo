"""Path, file-name, URL and text checks shared by every automation capability.

All file access goes through ``PathGuard``: a path is resolved (following
symlinks and junctions, so ``..`` tricks cannot escape), then it must lie
inside a permitted folder (with write access for changes) and outside the
protected locations (DUMBOO's own data, Windows, Program Files, AppData).
Files that typically hold credentials are never read or changed.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from automation.config import AutomationConfig
from security.secrets_filter import contains_secret, redact_secrets


class SafetyError(PermissionError):
    """A request was refused for safety reasons. ``str(exc)`` is safe to show to the user."""


class MissingPath(SafetyError):
    """The path is allowed but does not exist (yet). An earlier step of the same plan may create it."""


SECRET_FILE_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "id_rsa*", "id_dsa*", "id_ecdsa*",
                        "id_ed25519*", "*.kdbx", "*.kdb", "session.dat", "credentials", "credentials.*",
                        ".git-credentials", ".npmrc", ".pypirc", "*.ovpn", "wallet.dat", "*.keystore", "*.jks")

# Opening these through the shell would run code (or mount images); DUMBOO never does.
EXECUTABLE_EXTENSIONS = frozenset({
    ".exe", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".msi",
    ".msp", ".mst", ".scr", ".pif", ".lnk", ".url", ".hta", ".cpl", ".jar", ".py", ".pyw", ".pyz", ".reg", ".inf",
    ".application", ".appref-ms", ".gadget", ".msc", ".sh", ".appx", ".appxbundle", ".msix", ".msixbundle",
    ".scf", ".iso", ".img", ".vhd", ".vhdx", ".dll", ".sys", ".drv", ".ocx", ".library-ms", ".settingcontent-ms",
    ".xll", ".xlam", ".ppam", ".docm", ".xlsm", ".pptm", ".chm", ".diagcab", ".search-ms", ".theme", ".themepack",
})

_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def is_secret_file(path: Path) -> bool:
    name = path.name.lower()
    return any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_FILE_PATTERNS)


def is_executable(path: Path) -> bool:
    return path.suffix.lower() in EXECUTABLE_EXTENSIONS


def valid_name(name: str) -> str:
    """A single file or folder name (no folders inside), safe on Windows."""
    name = (name or "").strip()
    if not name or len(name) > 180:
        raise SafetyError("The name must be 1-180 characters long.")
    if _BAD_NAME_CHARS.search(name):
        raise SafetyError('Names cannot contain \\ / : * ? " < > | or control characters.')
    if name in (".", "..") or name.endswith((".", " ")):
        raise SafetyError("Names cannot be '.' or '..' or end with a dot or space.")
    if name.split(".")[0].lower() in _RESERVED:
        raise SafetyError(f"'{name}' is a reserved Windows device name.")
    return name


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class PathGuard:
    def __init__(self, config: Callable[[], AutomationConfig]) -> None:
        self._config = config

    def roots(self, write: bool = False) -> list[Path]:
        return [p.path for p in self._config().permitted if p.write or not write]

    def resolve(self, raw: str | os.PathLike) -> Path:
        text = str(raw).strip().strip('"')
        if not text:
            raise SafetyError("No path was given.")
        if text.startswith(("\\\\", "//")):
            raise SafetyError("Network and device paths are not allowed.")
        if re.search(r":.*:", text) or (":" in text and not re.match(r"^[A-Za-z]:[\\/]", text)):
            raise SafetyError("That path is not valid (alternate data streams are not allowed).")
        path = Path(os.path.expandvars(os.path.expanduser(text)))
        if not path.is_absolute():
            raise SafetyError("Please give the full path, for example C:\\Users\\you\\Documents\\notes.txt.")
        try:
            return path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise SafetyError(f"That path could not be resolved ({exc.__class__.__name__}).") from exc

    def check(self, raw: str | os.PathLike, *, write: bool = False, must_exist: bool = False,
              container: bool = False) -> Path:
        """Return the resolved path if access is allowed, else raise ``SafetyError``.

        ``container=True``: the folder only *receives* changes (files are created or moved into it), so
        a permitted folder itself (e.g. Downloads) is fine. Otherwise a permitted folder itself can't be
        renamed, moved or deleted.
        """
        path = self.resolve(raw)
        config = self._config()
        for part in path.parts[1:]:
            if part.split(".")[0].lower() in _RESERVED:
                raise SafetyError("That path uses a reserved Windows device name.")
        if config.is_protected(path):
            raise SafetyError("That location is protected (system, program, app-data or DUMBOO data files).")
        if is_secret_file(path):
            raise SafetyError("That file usually holds credentials or keys, so DUMBOO will not touch it.")
        roots = [p for p in config.permitted if p.write or not write]
        if not any(_within(path, r.path) for r in roots):
            kind = "write to" if write else "access"
            allowed = ", ".join(str(r.path) for r in roots) or "none"
            raise SafetyError(f"I'm not permitted to {kind} {path}. Permitted folders: {allowed}. "
                              "The owner can add folders on AUTOMATION → APPS & FOLDERS.")
        if write and not container and any(r.path == path for r in config.permitted):
            raise SafetyError("A permitted folder itself cannot be renamed, moved or deleted.")
        if must_exist and not path.exists():
            raise MissingPath(f"{path} does not exist.")
        return path

    def readable(self, raw: str | os.PathLike) -> bool:
        try:
            self.check(raw)
            return True
        except SafetyError:
            return False


def check_url(url: str, blocked_domains: list[str] | None = None) -> str:
    url = (url or "").strip()
    if re.match(r"^[\w.-]+\.[a-z]{2,}(/.*)?$", url, re.I):
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise SafetyError("Only http:// and https:// web addresses can be opened.")
    if not parts.hostname:
        raise SafetyError("That web address has no host name.")
    if parts.username or parts.password:
        raise SafetyError("Web addresses with embedded user names or passwords are not allowed.")
    host = parts.hostname.lower()
    for blocked in blocked_domains or []:
        blocked = blocked.lower().lstrip(".")
        if host == blocked or host.endswith("." + blocked):
            raise SafetyError(f"{host} is blocked in the automation settings.")
    if len(url) > 2048:
        raise SafetyError("That web address is too long.")
    return url


def os_error_message(exc: OSError) -> str:
    """A readable reason for a file-system error, recognising Windows' Controlled folder access."""
    import errno
    import sys

    name = getattr(exc, "filename", None)
    parent = Path(name).parent if name else None
    if exc.errno in (errno.ENOENT, errno.EACCES) and parent is not None and parent.exists() and sys.platform == "win32":
        return (f"Windows blocked writing to {parent}. This is usually Windows Security's “Controlled folder access” "
                "(ransomware protection), which protects Documents, Desktop, Pictures, Music and Videos. To allow "
                f"DUMBOO: Windows Security → Virus & threat protection → Manage ransomware protection → Allow an app "
                f"through Controlled folder access → add {sys.executable}")
    return f"{exc.strerror or exc.__class__.__name__}" + (f": {name}" if name else "")


def safe_text(value: Any, limit: int = 160) -> Any:
    """What may be written to task history and logs: secrets redacted, long text shortened."""
    if isinstance(value, str):
        if contains_secret(value):
            return "[redacted]"
        value = redact_secrets(value)
        return value if len(value) <= limit else value[:limit] + f"… ({len(value)} chars)"
    if isinstance(value, dict):
        return {k: safe_text(v, limit) for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        cap = 40 if limit < 1000 else 500
        items = [safe_text(v, limit) for v in list(value)[:cap]]
        return items + ([f"… {len(value) - cap} more"] if len(value) > cap else [])
    return value


def safe_args(capability: str, args: dict[str, Any], private_keys: tuple[str, ...] = ()) -> dict[str, Any]:
    """Arguments as stored in task history: private values (typed text, form data) are never stored."""
    out: dict[str, Any] = {}
    for key, value in args.items():
        if key in private_keys:
            out[key] = f"[{len(str(value))} characters, not stored]" if isinstance(value, str) else "[not stored]"
        else:
            out[key] = safe_text(value, 1000)
    return out
