"""Manual LIVE check of the configured AI provider (uses your API key, network and quota).

    python scripts/ai_check.py                      # provider/model from SETTINGS / .env
    python scripts/ai_check.py --provider openai    # check another provider
    python scripts/ai_check.py --model gemini-3.5-flash-lite
    python scripts/ai_check.py --health-only        # key/model check only: generates no text, uses no generation quota

Without --health-only it sends three short requests (about 3 requests of a few hundred tokens):
a one-line question, a follow-up that needs the conversation context, and a "detail" request.
This is NOT part of the automated test suite, which never contacts a real provider.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

# Windows consoles default to a legacy code page; never crash on symbols like "→".
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai import errors as E  # noqa: E402
from ai.base import AIRequest  # noqa: E402
from ai.config import AIConfig, api_key_for, env_overrides  # noqa: E402
from ai.context import ConversationContext  # noqa: E402
from ai.factory import create_provider  # noqa: E402
from ai.prompts import build_system_prompt, load_base_prompt  # noqa: E402
from services.settings_service import SettingsService  # noqa: E402
from utils.env import load_dotenv, mask  # noqa: E402
from utils.paths import default_paths  # noqa: E402

QUESTIONS = [
    "What is 15 × 8?",
    "Explain how you calculated it.",            # needs context from the previous turn
    "What is RAM? Explain in detail with an example.",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("gemini", "openai"))
    parser.add_argument("--model")
    parser.add_argument("--health-only", action="store_true")
    parser.add_argument("--no-stream", action="store_true")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    import os

    paths = default_paths()
    settings = SettingsService(paths.default_settings, paths.user_settings)
    section = settings.section("ai")
    section.update(env_overrides(os.environ)[0])
    if args.provider:
        section["provider"] = args.provider
    if args.model:
        section[f"{section['provider']}_model"] = args.model
    config = AIConfig.from_settings(section)
    if args.no_stream:
        config = replace(config, streaming=False)
    print(f"Provider: {config.provider}   model: {config.model}   key: {mask(api_key_for(config.provider))}")
    try:
        provider = create_provider(config)
    except E.ConfigError as exc:
        print(f"NOT CONFIGURED: {exc.user_message}")
        return 2

    status = provider.health_check()
    print(f"Health check: {'OK' if status.ok else 'FAILED'} - {status.message}"
          + (f" ({status.latency_s:.2f} s)" if status.latency_s else ""))
    if not status.ok or args.health_only:
        return 0 if status.ok else 1

    print("\nSending 3 short requests (uses your quota)…")
    base = load_base_prompt(config.system_prompt_path(ROOT))
    context = ConversationContext(config.context_turns, config.context_max_chars)
    for question in QUESTIONS:
        request = AIRequest(system_prompt=build_system_prompt(base, concise=config.concise, source="typed",
                                                              voice_output=False),
                            messages=context.messages_for(question), max_output_tokens=config.max_output_tokens,
                            temperature=config.temperature, timeout_s=config.timeout_s)
        print(f"\nYOU: {question}\nDUMBOO: ", end="", flush=True)
        start = time.monotonic()
        first = None
        try:
            if config.streaming:
                parts = []
                for chunk in provider.stream_response(request):
                    if chunk.text:
                        first = first or time.monotonic() - start
                        parts.append(chunk.text)
                        print(chunk.text, end="", flush=True)
                answer = "".join(parts).strip()
            else:
                answer = provider.generate_response(request).text
                print(answer, end="")
        except E.AIError as exc:
            print(f"\nFAILED ({exc.kind}): {exc.user_message}")
            return 1
        total = time.monotonic() - start
        print(f"\n   [{len(answer)} chars, first text after {first:.2f} s, total {total:.2f} s]" if first
              else f"\n   [{len(answer)} chars, {total:.2f} s]")
        context.add_turn(question, answer, "typed")
    print("\nLive check finished: key, model, streaming and follow-up context worked with this provider.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
