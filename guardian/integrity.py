"""File-integrity monitoring for folders and files the user chose (never the whole disk).

Baseline = SHA-256, size and modification time of every file in the scope. A later check reports
files that were **modified**, went **missing** or are **new**, each as a timestamped event. A change
is an observation, not proof of an attack, and nothing is ever deleted, quarantined or changed.
Windows, Program Files, other users' folders, DUMBOO's own data and drive roots are refused unless
the owner explicitly allows them in ``security.integrity_allowed_extra``.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path

from guardian.files import sha256_file
from guardian.models import GuardianError

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vs", "$recycle.bin"}


def refused_reason(path: Path, data_dir: Path) -> str | None:
    p = path.resolve()
    low = str(p).lower()
    windir = os.environ.get("WINDIR", r"C:\Windows").lower()
    if p == Path(p.anchor):
        return "a whole drive can't be monitored; choose a folder"
    if low.startswith(windir):
        return "Windows' own folders are protected by Windows itself and are not monitored"
    if low.startswith((r"c:\program files", r"c:\programdata")):
        return "program folders change with every update; monitor your own folders instead"
    try:
        if p.is_relative_to(data_dir.resolve()):
            return "DUMBOO's own data folder can't be monitored"
    except (OSError, ValueError):
        pass
    users = Path(os.environ.get("SYSTEMDRIVE", "C:") + "\\Users")
    home = Path.home().resolve()
    if low.startswith(str(users).lower()) and not p.is_relative_to(home) and low != str(users).lower():
        return "other users' folders are private"
    return None


def scan_files(root: Path, max_files: int, cancel: threading.Event | None = None) -> list[Path]:
    if root.is_file():
        return [root]
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
        for name in filenames:
            files.append(Path(dirpath) / name)
            if len(files) > max_files:
                raise GuardianError(f"more than {max_files:,} files: choose a smaller folder", "invalid")
        if cancel is not None and cancel.is_set():
            raise GuardianError("cancelled", "cancelled")
    return files


def snapshot(root: Path, max_files: int, max_mb: int, cancel: threading.Event | None = None) -> tuple[dict, list[str]]:
    """{relative path: (sha256, size, mtime)} and a list of files that couldn't be read."""
    out, skipped = {}, []
    base = root if root.is_dir() else root.parent
    for f in scan_files(root, max_files, cancel):
        rel = str(f.relative_to(base))
        try:
            st = f.stat()
            out[rel] = (sha256_file(f, max_mb, cancel), st.st_size,
                        datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"))
        except GuardianError as exc:
            if exc.kind == "cancelled":
                raise
            skipped.append(f"{rel}: {exc.user_message}")
        except OSError as exc:
            skipped.append(f"{rel}: {exc.__class__.__name__}")
    return out, skipped


def compare(baseline: dict, current: dict) -> list[dict]:
    events = []
    for rel, (sha, _size, _mtime) in current.items():
        if rel not in baseline:
            events.append({"kind": "new", "rel_path": rel, "old": None, "new": sha})
        elif baseline[rel][0] != sha:
            events.append({"kind": "modified", "rel_path": rel, "old": baseline[rel][0], "new": sha})
    for rel, (sha, _size, _mtime) in baseline.items():
        if rel not in current:
            events.append({"kind": "missing", "rel_path": rel, "old": sha, "new": None})
    return sorted(events, key=lambda e: (e["kind"], e["rel_path"]))
