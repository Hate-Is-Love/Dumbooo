"""Diagnostics report: versions, devices, models and service status.

Used by ``python main.py --diagnose`` and the dashboard's Diagnostics panel.
Contains no audio and no personal data beyond device names.
"""

from __future__ import annotations

import importlib.metadata
import os
import platform
import sys
from pathlib import Path

from audio import audio_devices
from audio.vad import silero_model_path
from utils.paths import AppPaths

PACKAGES = ("PySide6", "numpy", "sounddevice", "onnxruntime", "faster-whisper", "openwakeword", "piper-tts", "psutil",
            "google-genai", "openai")


def package_versions() -> dict[str, str]:
    versions = {}
    for name in PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "NOT INSTALLED"
    return versions


def model_files(paths: AppPaths, settings: dict) -> dict[str, str]:
    def state(p: Path) -> str:
        return f"found ({p})" if p.exists() else f"missing ({p})"

    piper = paths.resolve(settings["tts"]["voice_model"])
    wake = paths.resolve(settings["wake_word"]["model_path"])
    silero = silero_model_path()
    whisper_dir = paths.models_dir / "whisper"
    cached = sorted(p.name for p in whisper_dir.glob("models--*")) if whisper_dir.exists() else []
    return {
        "silero_vad": f"found ({silero})" if silero else "missing (install faster-whisper)",
        "piper_voice": state(piper),
        "piper_voice_config": state(Path(str(piper) + ".json")),
        "wake_word_custom_model": state(wake) + ("" if wake.exists() else " -> using Whisper phrase spotter"),
        "whisper_cache": ", ".join(cached) if cached else "empty (models download on first use)",
    }


def ai_summary(settings: dict) -> dict[str, str]:
    """Provider, model and whether keys are present. Keys themselves are never included."""
    from ai.config import AIConfig, api_keys_for, env_overrides
    from utils.env import mask

    ai = dict(settings.get("ai", {}))
    overrides, warnings = env_overrides(os.environ)
    ai.update(overrides)
    config = AIConfig.from_settings(ai)
    try:
        config.validate()
        state = "ready"
    except Exception as exc:  # ConfigError
        state = f"NOT READY: {getattr(exc, 'user_message', exc)}"
    return {
        "provider": config.provider,
        "model": config.model,
        "status": state,
        "groq_keys": ", ".join(mask(k) for k in api_keys_for("groq")) or "not set",
        "gemini_keys": ", ".join(mask(k) for k in api_keys_for("gemini")) or "not set",
        "openai_keys": ", ".join(mask(k) for k in api_keys_for("openai")) or "not set",
        "answer_order": " → ".join(f"{p} ({m})" for p, m, _k in config.routes()) or "none",
        "auto_switch": str(config.auto_switch),
        "streaming": str(config.streaming),
        "max_output_tokens": str(config.max_output_tokens),
        "set_by_environment": ", ".join(sorted(overrides)) or "nothing",
        "env_warnings": "; ".join(warnings) or "none",
    }


def collect(paths: AppPaths, settings: dict, services: dict | None = None) -> dict:
    devices = audio_devices.list_devices()
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "packages": package_versions(),
        "audio_backend_error": audio_devices.backend_error(),
        "input_devices": audio_devices.unique_labels([d for d in devices if d.max_input_channels > 0]),
        "output_devices": audio_devices.unique_labels([d for d in devices if d.max_output_channels > 0]),
        "default_input": next((d.label for d in devices if d.is_default_input), "none"),
        "default_output": next((d.label for d in devices if d.is_default_output), "none"),
        "models": model_files(paths, settings),
        "ai": ai_summary(settings),
        "services": services or {},
        "data_dir": str(paths.data_dir),
        "log_dir": str(paths.logs_dir),
    }


def format_report(report: dict) -> str:
    lines = ["DUMBOO diagnostics", "=" * 40]
    for key, value in report.items():
        if isinstance(value, dict):
            lines.append(f"{key}:")
            for k, v in value.items():
                if isinstance(v, dict):
                    v = ("OK  " if v.get("ok") else "FAIL") + f"  {v.get('detail', '')}"
                lines.append(f"  {k:<24} {v}")
        elif isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {item}" for item in value) if value else lines.append("  (none)")
        else:
            lines.append(f"{key:<26} {value}")
    return "\n".join(lines)
