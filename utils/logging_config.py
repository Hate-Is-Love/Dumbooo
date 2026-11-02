"""Logging setup: rotating file log plus console output.

Audio samples and transcripts of background audio are never logged. Only
events (for example "wake word detected, score 0.81") are written.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(threadName)-14s %(name)s: %(message)s"

# Anything that looks like an API key is masked before it reaches a log file or the console.
_KEY_PATTERNS = re.compile(r"(AIza[0-9A-Za-z_\-]{20,}|AQ\.[0-9A-Za-z_\-]{20,}|sk-[A-Za-z0-9_\-]{16,}"
                           r"|(?i:bearer)\s+[A-Za-z0-9._\-]{16,}"
                           r"|(?i:(?:api[_-]?key|key)=)[A-Za-z0-9_\-]{16,})")
_SECRET_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY")


def redact(text: str) -> str:
    for var in _SECRET_VARS:
        value = os.environ.get(var, "")
        if len(value) >= 8:
            text = text.replace(value, "[REDACTED]")
    return _KEY_PATTERNS.sub("[REDACTED]", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        if record.exc_info and not record.exc_text:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def configure_logging(logs_dir: Path, level: int = logging.INFO, console: bool = True) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "dumboo.log"
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    file_handler.addFilter(RedactingFilter())
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
        stream.addFilter(RedactingFilter())
        root.addHandler(stream)

    # Third-party libraries are chatty at INFO.
    for noisy in ("faster_whisper", "httpx", "httpx2", "httpcore", "urllib3", "huggingface_hub", "filelock",
                  "openai", "google_genai", "google.genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_file
