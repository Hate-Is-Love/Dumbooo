"""Running approved project commands (tests, dependency installs) with timeouts and cancellation.

Commands are always argument lists (never a shell string), run without a
shell, with a cleaned environment (no API keys), inside a validated working
folder. Output is captured (the last ``MAX_OUTPUT`` characters). On STOP,
emergency stop or timeout the whole process tree is terminated.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from automation.platform.desktop import clean_environment

log = logging.getLogger(__name__)

MAX_OUTPUT = 60_000


@dataclass
class CommandResult:
    argv: list[str]
    cwd: str
    exit_code: int | None
    output: str
    duration_s: float
    timed_out: bool = False
    cancelled: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled and not self.error

    def command_text(self) -> str:
        return " ".join(f'"{a}"' if " " in a else a for a in self.argv)


def kill_tree(pid: int) -> None:
    try:
        import psutil

        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for p in children + [parent]:
            try:
                p.terminate()
            except psutil.Error:
                pass
        _gone, alive = psutil.wait_procs(children + [parent], timeout=3)
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass
    except Exception:
        log.debug("kill_tree(%s) failed", pid, exc_info=True)


class ProcessRunner:
    def __init__(self) -> None:
        self._running: dict[int, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def run(self, argv: list[str], cwd: Path, timeout_s: float, cancel: threading.Event,
            extra_env: dict[str, str] | None = None) -> CommandResult:
        started = time.monotonic()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(argv, cwd=str(cwd), env=clean_environment(extra_env), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags)
        except FileNotFoundError:
            return CommandResult(argv, str(cwd), None, "", 0.0, error=f"{argv[0]} was not found on this computer.")
        except OSError as exc:
            return CommandResult(argv, str(cwd), None, "", 0.0, error=f"The command could not start: {exc}")
        with self._lock:
            self._running[proc.pid] = proc
        chunks: list[bytes] = []
        size = 0

        def reader() -> None:
            nonlocal size
            assert proc.stdout is not None
            for line in iter(proc.stdout.readline, b""):
                chunks.append(line)
                size += len(line)
                while size > MAX_OUTPUT * 2 and len(chunks) > 1:
                    size -= len(chunks.pop(0))

        thread = threading.Thread(target=reader, name="CommandOutput", daemon=True)
        thread.start()
        timed_out = cancelled = False
        while proc.poll() is None:
            if cancel.is_set():
                cancelled = True
                break
            if time.monotonic() - started > timeout_s:
                timed_out = True
                break
            time.sleep(0.1)
        if timed_out or cancelled:
            kill_tree(proc.pid)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        thread.join(timeout=2)
        with self._lock:
            self._running.pop(proc.pid, None)
        text = b"".join(chunks).decode("utf-8", errors="replace")[-MAX_OUTPUT:]
        return CommandResult(argv, str(cwd), proc.returncode if not (timed_out or cancelled) else None, text,
                             round(time.monotonic() - started, 2), timed_out, cancelled)

    def kill_all(self) -> int:
        with self._lock:
            procs = list(self._running.values())
        for proc in procs:
            kill_tree(proc.pid)
        return len(procs)
