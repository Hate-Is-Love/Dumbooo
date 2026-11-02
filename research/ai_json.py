"""Calling the AI for structured (JSON) output and parsing it defensively."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

Generate = Callable[..., str]


class AIUnavailable(RuntimeError):
    """The AI provider couldn't be used (not configured, quota, network …). ``str`` is user-readable."""


def extract_json(text: str) -> Any:
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON in the reply")
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    return json.loads(text[start:end + 1])


def ask_json(generate: Generate | None, system: str, prompt: str, max_tokens: int = 2048) -> Any:
    """One JSON request with one retry on unparseable output. Raises ``AIUnavailable``."""
    if generate is None:
        raise AIUnavailable("no AI provider is available")
    from ai.errors import AIError

    last_error = ""
    for attempt in range(2):
        try:
            reply = generate(system, prompt if attempt == 0 else
                             prompt + f"\n\nYour previous reply was not valid JSON ({last_error}). Reply with JSON only.",
                             max_output_tokens=max_tokens)
        except AIError as exc:
            raise AIUnavailable(exc.user_message) from exc
        try:
            return extract_json(reply)
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)[:80]
    raise AIUnavailable("the AI's answer could not be read (invalid JSON twice)")


def int_list(value: Any, limit: int = 12) -> list[int]:
    out: list[int] = []
    for v in value if isinstance(value, list) else []:
        try:
            n = int(str(v).strip("[] "))
        except ValueError:
            continue
        if n not in out:
            out.append(n)
    return out[:limit]


def text(value: Any, limit: int = 2000) -> str:
    return " ".join(str(value or "").split())[:limit]
