"""Create the AI provider from configuration.

With one usable route this is that provider itself. With several (other providers that
have keys, or extra keys) and ``auto_switch`` on, it is a ``FallbackProvider`` that
switches between them when one has a problem (see ``ai/fallback.py``).
"""

from __future__ import annotations

from ai.base import AIProvider
from ai.config import AIConfig


def build_single(config: AIConfig, provider: str, model: str, key: str) -> AIProvider:
    if provider == "gemini":
        from ai.providers.gemini_provider import GeminiProvider

        return GeminiProvider(key, model, config.gemini_thinking_level)
    if provider == "groq":
        from ai.providers.groq_provider import GroqProvider

        return GroqProvider(key, model, config.groq_reasoning_effort)
    if provider == "openai":
        from ai.providers.openai_provider import OpenAIProvider

        return OpenAIProvider(key, model, config.openai_reasoning_effort)
    from ai.providers.echo_provider import EchoProvider

    return EchoProvider()


def create_provider(config: AIConfig) -> AIProvider:
    config.validate()  # raises ConfigError with a helpful message (missing key, unknown provider…)
    routes = config.routes()
    if len(routes) == 1:
        return build_single(config, *routes[0])
    from ai.fallback import FallbackProvider

    return FallbackProvider(routes, lambda p, m, k: build_single(config, p, m, k),
                            switch_timeout_s=config.switch_timeout_s)
