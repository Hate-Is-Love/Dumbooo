"""Research configuration: provider choice, limits and keys.

Settings live in the ``research`` section of ``config/settings.json`` (changeable on
the RESEARCH page). API keys are only read from the environment / ``.env``:
``TAVILY_API_KEY`` or ``BRAVE_API_KEY``. Keys are never logged, stored in the
database, put in URLs or sent to the AI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

WEB_PROVIDERS = ("auto", "tavily", "brave", "browser", "off")
NEWS_PROVIDERS = ("auto", "tavily", "brave", "google_news", "off")
KEY_VARIABLES = {"tavily": "TAVILY_API_KEY", "brave": "BRAVE_API_KEY"}
KEY_HELP = {
    "tavily": "Free plan (1,000 searches/month, no card) at https://app.tavily.com -> set TAVILY_API_KEY in .env",
    "brave": "Brave Search API at https://api-dashboard.search.brave.com -> set BRAVE_API_KEY in .env",
}


@dataclass(frozen=True)
class ResearchConfig:
    web_provider: str = "auto"
    news_provider: str = "auto"
    keyless_fallback: bool = True          # browser-based web search when no search API key is set
    auto_research: bool = True             # research time-sensitive questions automatically
    region: str = "IN"
    language: str = "en"
    max_searches: int = 6
    max_pages: int = 8
    page_timeout_s: float = 15.0
    total_timeout_s: float = 150.0
    news_days: int = 7
    max_page_kb: int = 3072
    respect_robots: bool = True
    tavily_key: str = ""
    brave_key: str = ""

    @classmethod
    def from_settings(cls, section: Mapping[str, Any], environ: Mapping[str, str]) -> "ResearchConfig":
        values = {k: section[k] for k in cls.__dataclass_fields__ if k in section and not k.endswith("_key")}
        return cls(**values, tavily_key=(environ.get("TAVILY_API_KEY") or "").strip(),
                   brave_key=(environ.get("BRAVE_API_KEY") or "").strip())

    def key_for(self, provider: str) -> str:
        return {"tavily": self.tavily_key, "brave": self.brave_key}.get(provider, "")

    def describe(self) -> str:
        keys = [p for p in ("tavily", "brave") if self.key_for(p)]
        return (f"web: {self.web_provider} · news: {self.news_provider} · API keys: "
                f"{', '.join(keys) if keys else 'none (keyless sources only)'}")
