"""AI configuration: one common model plus provider-specific validation.

Where values come from (highest priority first):

1. Environment variables / ``.env`` (``AI_PROVIDER``, ``GEMINI_MODEL`` …), applied
   at start-up for that run only (see ``ENV_OVERRIDES``).
2. The dashboard's SETTINGS → AI section (saved in ``data/settings.json``).
3. Defaults in ``config/settings.json``.

API keys are only ever read from the environment (``GEMINI_API_KEY`` or
``GOOGLE_API_KEY``, ``GROQ_API_KEY``, ``OPENAI_API_KEY``), never from settings.json.
Extra keys for the same provider go in numbered variables (``GROQ_API_KEY_2`` …
``_9``). With ``auto_switch`` on, every provider that has a key is used, in
``fallback_order`` after the chosen provider (see ``ai/fallback.py``).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from ai.errors import ConfigError

log = logging.getLogger(__name__)

PROVIDERS = ("groq", "gemini", "openai", "echo")
REAL_PROVIDERS = ("groq", "gemini", "openai")
PROVIDER_LABELS = {"groq": "Groq (fastest)", "gemini": "Google Gemini", "openai": "OpenAI",
                   "echo": "Echo (offline test mode)"}
KEY_VARIABLES = {"gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "groq": ("GROQ_API_KEY",),
                 "openai": ("OPENAI_API_KEY",)}
MAX_EXTRA_KEYS = 8  # GROQ_API_KEY_2 … GROQ_API_KEY_9
KEY_HELP = {
    "gemini": "Create a free key at https://aistudio.google.com/apikey and set GEMINI_API_KEY.",
    "groq": "Create a free key at https://console.groq.com/keys and set GROQ_API_KEY.",
    "openai": ("Create a key at https://platform.openai.com/api-keys and set OPENAI_API_KEY. "
               "API usage is billed separately from a ChatGPT subscription."),
}

# Suggestions shown in the dashboard. Any model ID the account can use may be typed in.
MODEL_SUGGESTIONS = {
    "gemini": ["gemini-3.6-flash", "gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
               "gemini-flash-latest"],
    "groq": ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"],
    "openai": ["gpt-5-mini", "gpt-5-nano", "gpt-6-luna", "gpt-5.4-mini"],
}


def _bool(value: str) -> bool:
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"not a boolean: {value!r}")


def _float_or_none(value: str) -> float | None:
    v = value.strip().lower()
    return None if v in ("", "default", "none") else float(v)


# environment variable -> (settings key in the "ai" section, parser)
ENV_OVERRIDES: dict[str, tuple[str, Any]] = {
    "AI_PROVIDER": ("provider", lambda v: v.strip().lower()),
    "GEMINI_MODEL": ("gemini_model", str.strip),
    "GROQ_MODEL": ("groq_model", str.strip),
    "OPENAI_MODEL": ("openai_model", str.strip),
    "AI_REQUEST_TIMEOUT": ("timeout_s", float),
    "AI_MAX_OUTPUT_TOKENS": ("max_output_tokens", int),
    "AI_TEMPERATURE": ("temperature", _float_or_none),
    "AI_STREAMING_ENABLED": ("streaming", _bool),
}


def env_overrides(environ: Mapping[str, str]) -> tuple[dict[str, Any], list[str]]:
    """Parse the supported environment variables. Returns (values, warnings)."""
    values, warnings = {}, []
    for var, (key, parse) in ENV_OVERRIDES.items():
        raw = environ.get(var)
        if raw is None or raw.strip() == "":
            continue
        try:
            values[key] = parse(raw)
        except ValueError:
            warnings.append(f"{var} has an invalid value and was ignored")
    return values, warnings


def key_variables(provider: str) -> list[str]:
    """All variables that may hold a key for ``provider``, main one first."""
    names = list(KEY_VARIABLES.get(provider, ()))
    if names:
        names += [f"{names[0]}_{n}" for n in range(2, 2 + MAX_EXTRA_KEYS)]
    return names


def api_keys_for(provider: str, environ: Mapping[str, str] | None = None) -> list[str]:
    """Every distinct key set for ``provider`` (main key first)."""
    environ = os.environ if environ is None else environ
    keys: list[str] = []
    for var in key_variables(provider):
        value = (environ.get(var) or "").strip()
        if value and not value.startswith("your_") and value not in keys:  # skip .env.example placeholders
            keys.append(value)
    return keys


def api_key_for(provider: str, environ: Mapping[str, str] | None = None) -> str | None:
    keys = api_keys_for(provider, environ)
    return keys[0] if keys else None


@dataclass(frozen=True)
class AIConfig:
    provider: str = "groq"
    gemini_model: str = "gemini-3.6-flash"
    groq_model: str = "openai/gpt-oss-120b"
    openai_model: str = "gpt-5-mini"
    max_output_tokens: int = 1024
    temperature: float | None = None
    streaming: bool = True
    timeout_s: float = 30.0
    context_turns: int = 8
    context_max_chars: int = 12000
    concise: bool = True
    system_prompt_file: str = "config/system_prompt.txt"
    gemini_thinking_level: str = "low"
    openai_reasoning_effort: str = "low"
    groq_reasoning_effort: str = "low"
    max_retries: int = 2
    auto_switch: bool = True                      # use the other providers/keys when one has a problem
    fallback_order: str = "groq,gemini,openai"
    switch_timeout_s: float = 12.0                # give up on a slow provider sooner when another is ready
    api_key: str | None = None  # resolved from the environment; never saved, never logged
    keys: tuple[tuple[str, str], ...] = ()        # (provider, key) for every key set; never saved, never logged

    @classmethod
    def from_settings(cls, section: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> "AIConfig":
        fields = {k: section[k] for k in cls.__dataclass_fields__ if k in section and k not in ("api_key", "keys")}
        config = cls(**fields)
        keys = tuple((p, k) for p in REAL_PROVIDERS for k in api_keys_for(p, environ))
        return replace(config, api_key=api_key_for(config.provider, environ), keys=keys)

    @property
    def model(self) -> str:
        return self.model_for(self.provider)

    def model_for(self, provider: str) -> str:
        return {"gemini": self.gemini_model, "groq": self.groq_model,
                "openai": self.openai_model}.get(provider, "echo")

    def provider_order(self) -> list[str]:
        """The chosen provider first, then the others in ``fallback_order`` (if switching is on)."""
        if self.provider == "echo":
            return ["echo"]
        order = [self.provider]
        if self.auto_switch:
            listed = [p.strip().lower() for p in self.fallback_order.split(",")]
            order += [p for p in listed + list(REAL_PROVIDERS) if p in REAL_PROVIDERS]
        return list(dict.fromkeys(order))

    def routes(self) -> list[tuple[str, str, str]]:
        """(provider, model, key) in the order they are tried: only providers with a key and a model.

        Without ``auto_switch`` only the chosen provider's main key is used (the old behaviour).
        """
        if self.provider == "echo":
            return [("echo", "echo", "")]
        out = []
        for provider in self.provider_order():
            model = self.model_for(provider).strip()
            if model:
                out += [(provider, model, key) for p, key in self.keys if p == provider]
        if not self.auto_switch:
            out = out[:1] if out and out[0][0] == self.provider else []
        if not out and self.api_key and self.model.strip():  # keys given directly (tests, older callers)
            out = [(self.provider, self.model, self.api_key)]
        return out

    def validate(self) -> None:
        """Raise ``ConfigError`` with a beginner-friendly message if this provider cannot run."""
        if self.provider not in PROVIDERS:
            raise ConfigError(f"Unknown AI provider '{self.provider}'. Use gemini or openai.")
        if self.provider == "echo":
            return
        if not self.model.strip():
            raise ConfigError(f"No model is set for {PROVIDER_LABELS[self.provider]}.")
        if not self.api_key and not (self.auto_switch and self.routes()):
            label = PROVIDER_LABELS[self.provider]
            raise ConfigError(f"{label} API key is missing. {KEY_HELP[self.provider]} "
                              "You can also paste it in SETTINGS → AI.")

    def describe(self) -> str:
        """Safe, human-readable summary (no secrets)."""
        if self.provider == "echo":
            return "echo test mode (no AI connected)"
        key = "key set" if self.api_key else "NO API KEY"
        text = f"{self.provider} · {self.model} · {key}"
        routes = self.routes()
        backups = [p for p in dict.fromkeys(p for p, _m, _k in routes) if p != self.provider]
        extra = sum(1 for p, _m, _k in routes if p == self.provider) - (1 if self.api_key else 0)
        if self.auto_switch and (backups or extra > 0):
            parts = ([f"{extra} extra key(s)"] if extra > 0 else []) + backups
            text += " · switches to " + ", ".join(parts)
        return text

    def system_prompt_path(self, root: Path) -> Path:
        p = Path(self.system_prompt_file)
        return p if p.is_absolute() else root / p
