"""System prompt: the editable base text plus small, fixed style notes.

The base text lives in ``config/system_prompt.txt`` (edit it freely). It is
sent once per request in the provider's dedicated system/instructions field,
never copied into the conversation history.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

FALLBACK_PROMPT = (
    "You are DUMBOO, a personal desktop assistant. Answer the actual question first, in one to three "
    "short sentences, in plain language. Give more detail only when asked. Never invent facts; say when "
    "you are unsure."
)

DETAILED_NOTE = ("Concise mode is OFF: give complete, well-structured answers, but still lead with the "
                 "direct answer.")
VOICE_NOTE = ("The user spoke this message and will HEAR your reply through text-to-speech. Use natural "
              "spoken sentences: no tables, no headings, no Markdown symbols, no emoji. Avoid code unless asked; "
              "if code is needed, keep it short and say it is shown on screen.")
TEXT_NOTE = "The user typed this message; your reply is shown in a chat panel, so light Markdown is fine."


def load_base_prompt(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        log.warning("System prompt file %s not readable; using built-in prompt", path)
        return FALLBACK_PROMPT
    return text or FALLBACK_PROMPT


def build_system_prompt(base: str, *, concise: bool, source: str, voice_output: bool) -> str:
    parts = [base.strip()]
    if not concise:
        parts.append(DETAILED_NOTE)
    parts.append(VOICE_NOTE if source == "voice" else TEXT_NOTE)
    if source != "voice" and voice_output:
        parts.append("Your reply will also be read aloud, so prefer sentences over large tables.")
    return "\n\n".join(parts)
