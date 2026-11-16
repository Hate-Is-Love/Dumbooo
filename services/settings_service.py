"""Validated, persistent application settings.

Defaults ship in ``config/settings.json`` (part of the source). The user's
changes are stored separately in ``data/settings.json``. Every value is
checked against ``SCHEMA`` before it is applied or saved, so a typo in the
settings file can never crash the audio pipeline.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)


class SettingsError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class Field:
    kind: type | tuple[type, ...]
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple | None = None
    nullable: bool = False
    check: Callable[[Any], str | None] | None = None

    def validate(self, name: str, value: Any) -> str | None:
        if value is None:
            return None if self.nullable else f"{name} must not be empty"
        # bool is a subclass of int; keep them apart.
        if self.kind in (int, float, (int, float)) and isinstance(value, bool):
            return f"{name} must be a number"
        if not isinstance(value, self.kind):
            return f"{name} has the wrong type ({type(value).__name__})"
        if self.minimum is not None and value < self.minimum:
            return f"{name} must be at least {self.minimum}"
        if self.maximum is not None and value > self.maximum:
            return f"{name} must be at most {self.maximum}"
        if self.choices is not None and value not in self.choices:
            return f"{name} must be one of {', '.join(map(str, self.choices))}"
        if self.check is not None:
            return self.check(value)
        return None


def _phrase_check(value: str) -> str | None:
    words = value.strip().split()
    if not 1 <= len(words) <= 4:
        return "wake phrase must have 1 to 4 words"
    return None


def _model_check(value: str) -> str | None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}", value.strip()):
        return "model name may contain only letters, digits and . _ : / - (max 100 characters)"
    return None


NUM = (int, float)


def _order_check(value: str) -> str | None:
    names = [v.strip() for v in value.split(",") if v.strip()]
    if not names or any(n not in ("groq", "gemini", "openai") for n in names):
        return "ai.fallback_order must list providers from: groq, gemini, openai (comma-separated)"
    return None
WHISPER_MODELS = ("tiny.en", "tiny", "base.en", "base", "small.en", "small", "medium.en", "medium",
                  "large-v3", "distil-large-v3", "turbo")

SCHEMA: dict[str, dict[str, Field]] = {
    "audio": {
        "input_device": Field(str, nullable=True),
        "output_device": Field(str, nullable=True),
        "noise_suppression": Field(bool),
        "noise_suppression_strength": Field(NUM, 0.0, 1.0),
    },
    "wake_word": {
        "enabled": Field(bool),
        "phrase": Field(str, check=_phrase_check),
        "engine": Field(str, choices=("auto", "openwakeword", "whisper_spotter")),
        "model_path": Field(str),
        "spotter_model": Field(str, choices=WHISPER_MODELS),
        "sensitivity": Field(NUM, 0.05, 0.95),
        "cooldown_s": Field(NUM, 0.5, 10.0),
    },
    "vad": {
        "threshold": Field(NUM, 0.1, 0.95),
        "end_silence_ms": Field(int, 200, 3000),
        "pre_roll_ms": Field(int, 0, 1500),
        "min_speech_ms": Field(int, 100, 2000),
        "max_record_s": Field(NUM, 2, 60),
        "listen_timeout_s": Field(NUM, 2, 60),
    },
    "stt": {
        "enabled": Field(bool),
        "model": Field(str, choices=WHISPER_MODELS),
        "language": Field(str, check=lambda v: None if 2 <= len(v) <= 5 else "language must be a code like 'en'"),
        "device": Field(str, choices=("cpu", "cuda", "auto")),
        "compute_type": Field(str, choices=("int8", "int8_float16", "float16", "float32", "default")),
    },
    "tts": {
        "enabled": Field(bool),
        "voice_model": Field(str),
        "speed": Field(NUM, 0.5, 2.0),
    },
    "interruption": {
        "enabled": Field(bool),
        "threshold": Field(NUM, 0.3, 0.98),
        "min_speech_ms": Field(int, 100, 2000),
        "echo_margin_db": Field(NUM, 0.0, 20.0),
    },
    "dashboard": {
        "start_mode": Field(str, choices=("sleep", "visible")),
        "auto_sleep": Field(bool),
        "sleep_timeout_s": Field(int, 10, 3600),
        "animation_intensity": Field(NUM, 0.0, 1.0),
        "blob_enabled": Field(bool),
        "blob_sensitivity": Field(NUM, 0.0, 3.0),
        "text_only": Field(bool),
    },
    "accounts": {
        "owner_title_default": Field(str, check=lambda v: None if 1 <= len(v.strip()) <= 40 else "title must be 1-40 characters"),
        "registration_enabled": Field(bool),
        "session_timeout_min": Field(int, 1, 1440),
        "lockout_threshold": Field(int, 3, 20),
        "lockout_minutes": Field(int, 1, 1440),
        "login_attempt_retention_days": Field(int, 1, 365),
        "remember_me_allowed": Field(bool),
        "remember_me_days": Field(int, 1, 90),
        "history_retention_days": Field(int, 0, 3650),
        "memory_auto_extract": Field(bool),
        "memory_ai_learning": Field(bool),
        "face_unlock_enabled": Field(bool),
        "retrieval_memories": Field(int, 0, 20),
        "retrieval_messages": Field(int, 0, 20),
    },
    "security": {
        "monitor_enabled": Field(bool),
        "external_lookups": Field(bool),
        "threat_intel_provider": Field(str, choices=("none", "virustotal", "malwarebazaar")),
        "advisory_feeds": Field(bool),
        "scan_timeout_s": Field(NUM, 10, 600),
        "file_integrity_enabled": Field(bool),
    },
    "bridge": {
        "enabled": Field(bool),
        "port": Field(int, 1024, 65535),
        "allow_public": Field(bool),
    },
    "research": {
        "web_provider": Field(str, choices=("auto", "tavily", "brave", "browser", "off")),
        "news_provider": Field(str, choices=("auto", "tavily", "brave", "google_news", "off")),
        "keyless_fallback": Field(bool),
        "auto_research": Field(bool),
        "region": Field(str, check=lambda v: None if len(v) == 2 and v.isalpha() else "region must be a 2-letter "
                                                                                        "country code like IN"),
        "language": Field(str, check=lambda v: None if len(v) == 2 and v.isalpha() else "language must be a code "
                                                                                          "like en"),
        "max_searches": Field(int, 1, 20),
        "max_pages": Field(int, 1, 20),
        "page_timeout_s": Field(NUM, 3, 60),
        "total_timeout_s": Field(NUM, 20, 600),
        "news_days": Field(int, 1, 60),
        "max_page_kb": Field(int, 256, 20480),
        "respect_robots": Field(bool),
    },
    "ai": {
        "provider": Field(str, choices=("groq", "gemini", "openai", "echo")),
        "gemini_model": Field(str, check=_model_check),
        "groq_model": Field(str, check=_model_check),
        "openai_model": Field(str, check=_model_check),
        "max_output_tokens": Field(int, 32, 16384),
        "temperature": Field(NUM, 0.0, 2.0, nullable=True),
        "streaming": Field(bool),
        "timeout_s": Field(NUM, 5, 180),
        "context_turns": Field(int, 0, 50),
        "context_max_chars": Field(int, 1000, 200000),
        "concise": Field(bool),
        "system_prompt_file": Field(str),
        "gemini_thinking_level": Field(str, choices=("", "minimal", "low", "medium", "high")),
        "openai_reasoning_effort": Field(str, choices=("", "minimal", "low", "medium", "high")),
        "max_retries": Field(int, 0, 3),
        "groq_reasoning_effort": Field(str, choices=("", "low", "medium", "high")),
        "auto_switch": Field(bool),
        "fallback_order": Field(str, check=_order_check),
        "switch_timeout_s": Field(NUM, 3, 60),
    },
}


def validate(settings: dict) -> list[str]:
    errors: list[str] = []
    for section, fields in SCHEMA.items():
        values = settings.get(section)
        if not isinstance(values, dict):
            errors.append(f"missing section '{section}'")
            continue
        for key, spec in fields.items():
            if key not in values:
                errors.append(f"missing setting {section}.{key}")
                continue
            err = spec.validate(f"{section}.{key}", values[key])
            if err:
                errors.append(err)
    vad = settings.get("vad", {})
    if isinstance(vad, dict) and all(isinstance(vad.get(k), (int, float)) for k in ("min_speech_ms", "max_record_s")):
        if vad["min_speech_ms"] / 1000 >= vad["max_record_s"]:
            errors.append("vad.min_speech_ms must be shorter than vad.max_record_s")
    return errors


class SettingsService:
    name = "settings"

    def __init__(self, defaults_path: Path, user_path: Path, on_change: Callable[[dict, list[str]], None] | None = None) -> None:
        self._defaults_path = defaults_path
        self._user_path = user_path
        self._on_change = on_change
        self._lock = threading.RLock()
        self._session_overrides: dict[tuple[str, str], Any] = {}
        self._defaults = self._read_json(defaults_path)
        errors = validate(self._defaults)
        if errors:
            raise SettingsError([f"default settings invalid: {e}" for e in errors])
        self._settings = self._load_user_overrides()

    @staticmethod
    def _read_json(path: Path) -> dict:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def _load_user_overrides(self) -> dict:
        merged = copy.deepcopy(self._defaults)
        if not self._user_path.exists():
            return merged
        try:
            user = self._read_json(self._user_path)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Ignoring unreadable user settings %s: %s", self._user_path, exc)
            return merged
        for section, values in user.items():
            if section not in SCHEMA or not isinstance(values, dict):
                continue
            for key, value in values.items():
                spec = SCHEMA[section].get(key)
                if spec is None:
                    continue
                err = spec.validate(f"{section}.{key}", value)
                if err:
                    log.warning("Ignoring invalid saved setting: %s", err)
                    continue
                merged[section][key] = value
        if validate(merged):
            log.warning("Saved settings conflict with each other; using defaults")
            return copy.deepcopy(self._defaults)
        return merged

    # -- reading ----------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._settings)

    def defaults(self) -> dict:
        return copy.deepcopy(self._defaults)

    def get(self, section: str, key: str) -> Any:
        with self._lock:
            return self._settings[section][key]

    def section(self, section: str) -> dict:
        with self._lock:
            return dict(self._settings[section])

    # -- writing ----------------------------------------------------------
    def update(self, changes: dict[str, dict[str, Any]]) -> list[str]:
        """Validate and apply ``{section: {key: value}}``. Returns changed keys.

        Raises ``SettingsError`` and leaves settings untouched if any value is
        invalid.
        """
        with self._lock:
            candidate = copy.deepcopy(self._settings)
            errors: list[str] = []
            for section, values in changes.items():
                if section not in SCHEMA:
                    errors.append(f"unknown section '{section}'")
                    continue
                for key, value in values.items():
                    if key not in SCHEMA[section]:
                        errors.append(f"unknown setting {section}.{key}")
                        continue
                    candidate[section][key] = value
                    if value != self._settings[section][key]:
                        self._session_overrides.pop((section, key), None)  # explicit change wins
            errors += validate(candidate)
            if errors:
                raise SettingsError(errors)
            changed = [
                f"{s}.{k}" for s in SCHEMA for k in SCHEMA[s]
                if candidate[s][k] != self._settings[s][k]
            ]
            if not changed:
                return []
            self._settings = candidate
            self._save()
            snapshot = copy.deepcopy(candidate)
        if self._on_change:
            self._on_change(snapshot, changed)
        return changed

    def override_for_session(self, section: str, key: str, value: Any) -> None:
        """Apply a validated value for this run only (command-line flags); not saved."""
        err = SCHEMA[section][key].validate(f"{section}.{key}", value)
        if err:
            raise SettingsError([err])
        with self._lock:
            self._session_overrides.setdefault((section, key), self._settings[section][key])
            self._settings[section][key] = value

    def reset_to_defaults(self) -> list[str]:
        return self.update(copy.deepcopy(self._defaults))

    def _save(self) -> None:
        self._user_path.parent.mkdir(parents=True, exist_ok=True)
        persisted = copy.deepcopy(self._settings)
        for (section, key), saved_value in self._session_overrides.items():
            persisted[section][key] = saved_value
        tmp = self._user_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(persisted, fh, indent=2)
        tmp.replace(self._user_path)
