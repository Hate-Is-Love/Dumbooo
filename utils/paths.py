"""Filesystem locations.

Source code lives in the project folder. Everything DUMBOO writes at runtime
(database, user settings, logs, downloaded models) goes into git-ignored
folders so it never mixes with source files. ``DUMBOO_DATA_DIR`` overrides the
runtime data folder (tests use this to work in a temporary directory).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class AppPaths:
    root: Path
    data_dir: Path
    logs_dir: Path
    models_dir: Path

    @property
    def default_settings(self) -> Path:
        return self.root / "config" / "settings.json"

    @property
    def user_settings(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def database(self) -> Path:
        return self.data_dir / "dumboo.db"

    def resolve(self, path: str | os.PathLike) -> Path:
        """Resolve a settings path relative to the project root."""
        p = Path(path)
        return p if p.is_absolute() else self.root / p

    def ensure(self) -> "AppPaths":
        for d in (self.data_dir, self.logs_dir, self.models_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self


def default_paths() -> AppPaths:
    data = os.environ.get("DUMBOO_DATA_DIR")
    data_dir = Path(data) if data else PROJECT_ROOT / "data"
    logs = os.environ.get("DUMBOO_LOG_DIR")
    logs_dir = Path(logs) if logs else PROJECT_ROOT / "logs"
    return AppPaths(PROJECT_ROOT, data_dir, logs_dir, PROJECT_ROOT / "models")
