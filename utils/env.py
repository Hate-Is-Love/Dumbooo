"""Minimal ``.env`` support and secret masking (no extra dependency).

``load_dotenv`` reads ``KEY=value`` lines into ``os.environ`` without
overriding variables that are already set, so a real environment variable
always wins over the file. ``set_env_value`` lets the dashboard store an API
key in the local ``.env`` file (git-ignored) instead of in settings.json.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import MutableMapping

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    if " #" in value:  # inline comment on an unquoted value
        value = value.split(" #", 1)[0].rstrip()
    return value


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _LINE.match(line)
        if match:
            values[match.group(1)] = _unquote(match.group(2))
    return values


def load_dotenv(path: Path, environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Load ``path`` into ``environ`` (default ``os.environ``). Returns the keys loaded."""
    environ = os.environ if environ is None else environ
    loaded = []
    for key, value in read_env_file(path).items():
        if key not in environ and value != "":
            environ[key] = value
            loaded.append(key)
    return loaded


def set_env_value(path: Path, key: str, value: str, environ: MutableMapping[str, str] | None = None) -> None:
    """Create or replace ``key`` in the ``.env`` file and the live environment."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise ValueError("invalid variable name")
    if "\n" in value or "\r" in value:
        raise ValueError("value must be a single line")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, replaced = [], False
    for line in lines:
        match = _LINE.match(line)
        if match and match.group(1) == key:
            if not replaced:
                out.append(f"{key}={value}")
                replaced = True
            continue
        out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    environ = os.environ if environ is None else environ
    if value:
        environ[key] = value
    else:
        environ.pop(key, None)


def mask(secret: str | None) -> str:
    """Show only that a secret exists and its last 4 characters."""
    if not secret:
        return "not set"
    return f"set (…{secret[-4:]})" if len(secret) > 8 else "set"
