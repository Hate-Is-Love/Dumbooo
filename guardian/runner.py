"""Runs DUMBOO's own fixed, read-only PowerShell queries (Windows).

Only scripts defined in this package can run: the AI, chat text, file names or process command
lines are never put into a script. Values a script needs (file paths) are passed as JSON through
an environment variable, never spliced into the command text. Every run has a timeout, uses
``-NoProfile -NonInteractive``, and its output is parsed as JSON.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from typing import Any, Callable

from guardian.models import GuardianError

log = logging.getLogger(__name__)

PREAMBLE = ("$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; ")


class PowerShell:
    """``run(script, data)`` -> parsed JSON. Injectable for tests (``FakePowerShell``)."""

    def __init__(self, executable: str = "powershell.exe") -> None:
        self._exe = executable

    @property
    def available(self) -> bool:
        return sys.platform == "win32"

    def run(self, script: str, data: Any = None, timeout: float = 60.0) -> Any:
        if not self.available:
            raise GuardianError("PowerShell is only available on Windows", "unavailable")
        env = dict(os.environ)
        env["DUMBOO_ARGS"] = json.dumps(data if data is not None else {})
        command = PREAMBLE + "$A = $env:DUMBOO_ARGS | ConvertFrom-Json; " + script
        try:
            done = subprocess.run([self._exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                   "-Command", command], capture_output=True, timeout=timeout, env=env,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise GuardianError(f"the system query did not finish within {timeout:.0f} s", "timeout") from exc
        except OSError as exc:
            raise GuardianError(f"PowerShell could not be started ({exc.__class__.__name__})") from exc
        out = done.stdout.decode("utf-8", "replace").strip()
        err = done.stderr.decode("utf-8", "replace").strip()
        if done.returncode != 0:
            low = err.lower()
            kind = "permission" if ("access is denied" in low or "requires elevation" in low
                                    or "administrator" in low or "0x80041003" in low) else "unavailable"
            log.info("Security query failed (%s): %s", kind, err[:200].replace("\n", " "))
            raise GuardianError(_first_line(err) or "the system query failed", kind)
        if not out:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise GuardianError("the system query returned unreadable output", "unavailable") from exc


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("At line", "+ ", "    +")):
            return line[:200]
    return ""


def as_list(value: Any) -> list:
    """PowerShell's ConvertTo-Json gives an object for one item and a list for several."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


RunFn = Callable[[str, Any, float], Any]
