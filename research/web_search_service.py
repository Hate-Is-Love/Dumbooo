"""Running searches: provider selection, caching, one careful retry, ranking and honest notes.

Provider choice (``research.web_provider`` / ``research.news_provider``):

* ``auto``: an API provider whose key is configured (Tavily, then Brave). Without a key,
  web search uses DUMBOO's headless browser (if ``keyless_fallback`` is on) and news uses
  the keyless Google News RSS feed plus GDELT. The answer always names what was used.
* a named provider: only that one. If its key is missing, the search fails with a clear
  message — DUMBOO never silently switches to a different (possibly paid) service.
* ``off``: no live search; DUMBOO says so instead of answering from old knowledge.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from research.config import KEY_HELP, KEY_VARIABLES, ResearchConfig
from research.models import SearchResult
from research.quality import rank
from research.search_provider import SearchError, SearchProvider

log = logging.getLogger(__name__)

CACHE_TTL = {"web": 3600.0, "news": 600.0}
MAX_RETRY_WAIT_S = 8.0


@dataclass
class SearchOutcome:
    results: list[SearchResult]
    providers: list[str]
    notes: list[str] = field(default_factory=list)
    errors: list[SearchError] = field(default_factory=list)


class WebSearchService:
    def __init__(self, config: Callable[[], ResearchConfig], providers: dict[str, SearchProvider],
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self._config = config
        self._providers = providers
        self._clock = clock
        self._sleep = sleep
        self._cache: dict[tuple, tuple[float, list[SearchResult]]] = {}
        self._lock = threading.Lock()
        self.calls = 0  # real provider calls (not cache hits), used in tests and status

    # -- provider choice ---------------------------------------------------------------------
    def chain(self, kind: str) -> tuple[list[str], list[str]]:
        """Providers to use for ``kind`` (in order) and notes explaining the choice."""
        cfg = self._config()
        choice = cfg.web_provider if kind == "web" else cfg.news_provider
        if choice == "off":
            return [], [f"Live {'web' if kind == 'web' else 'news'} search is switched off in the research settings."]
        if choice != "auto":
            return [choice], []
        keyed = [p for p in ("tavily", "brave") if cfg.key_for(p)]
        if keyed:
            return keyed[:1], []
        if kind == "news":
            return ["google_news", "gdelt"], ["News came from the keyless Google News feed and the GDELT news index."]
        if cfg.keyless_fallback:
            return ["browser"], ["No search API key is configured, so DUMBOO searched with its own browser (Bing "
                                 "results page). For more reliable results add a free search key (see README 17.1)."]
        return [], ["No search API key is configured and the keyless fallback is off. " + KEY_HELP["tavily"]]

    def status(self) -> dict:
        web, _ = self.chain("web")
        news, _ = self.chain("news")
        return {"web": web, "news": news, "configured": bool(web or news)}

    # -- searching -------------------------------------------------------------------------------
    def _one(self, provider: SearchProvider, query: str, kind: str, max_results: int, days: int | None,
             cancel: threading.Event | None, fresh: bool = False) -> list[SearchResult]:
        cfg = self._config()
        key = (provider.name, kind, query.strip().lower(), max_results, days, cfg.region, cfg.language)
        now = self._clock()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < CACHE_TTL[kind] and not fresh:
                return list(hit[1])
        attempt = 0
        while True:
            try:
                self.calls += 1
                results = provider.search(query, kind=kind, max_results=max_results, days=days, region=cfg.region,
                                          language=cfg.language)
                break
            except SearchError as exc:
                wait = exc.retry_after if exc.retry_after is not None else 1.5
                if attempt >= 1 or not exc.retryable or wait > MAX_RETRY_WAIT_S or (cancel and cancel.is_set()):
                    raise
                attempt += 1
                log.info("Search via %s failed (%s); retrying once in %.1fs", provider.name, exc.kind, wait)
                if cancel is not None and cancel.wait(wait):
                    raise
                if cancel is None:
                    self._sleep(wait)
        with self._lock:
            self._cache[key] = (self._clock(), list(results))
            if len(self._cache) > 300:
                oldest = sorted(self._cache.items(), key=lambda kv: kv[1][0])[:100]
                for k, _v in oldest:
                    self._cache.pop(k, None)
        return results

    def search(self, query: str, kind: str = "web", *, max_results: int = 10, days: int | None = None,
               cancel: threading.Event | None = None, fresh: bool = False) -> SearchOutcome:
        """``fresh=True`` skips the cache (used when the user asks to refresh research)."""
        chain, notes = self.chain(kind)
        outcome = SearchOutcome([], [], list(notes))
        for name in chain:
            provider = self._providers.get(name)
            if provider is None:
                outcome.errors.append(SearchError("config", name, "provider not available"))
                continue
            if name in KEY_VARIABLES and not self._config().key_for(name):
                err = SearchError("config", name, f"{KEY_VARIABLES[name]} is not set")
                outcome.errors.append(err)
                outcome.notes.append(f"{name} is selected but not configured: {KEY_HELP.get(name, '')}")
                continue
            try:
                found = self._one(provider, query, kind, max_results, days, cancel, fresh)
            except SearchError as exc:
                log.warning("Search via %s failed: %s", name, exc.kind)
                outcome.errors.append(exc)
                continue
            outcome.providers.append(name)
            outcome.results.extend(found)
        outcome.results = rank(outcome.results, query, news=(kind == "news"))
        return outcome
