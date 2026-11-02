"""Brave Search API (https://api-dashboard.search.brave.com) — independent web index with web and news search.

Needs ``BRAVE_API_KEY`` (sent as the ``X-Subscription-Token`` header).
"""

from __future__ import annotations

from datetime import datetime, timezone

from research.dates import parse_date, relative_age
from research.http import ClientFactory, default_client, request
from research.models import SearchResult
from research.search_provider import ProviderStatus, SearchError, SearchProvider

BASE = "https://api.search.brave.com/res/v1"


def _freshness(days: int | None) -> str | None:
    if not days:
        return None
    return "pd" if days <= 1 else "pw" if days <= 7 else "pm" if days <= 31 else "py"


class BraveProvider(SearchProvider):
    name = "brave"
    kinds = ("web", "news")
    needs_key = True

    def __init__(self, api_key: str, client_factory: ClientFactory = default_client, timeout: float = 20.0) -> None:
        self._key = api_key
        self._client = client_factory
        self._timeout = timeout

    def status(self) -> ProviderStatus:
        return ProviderStatus(self.name, bool(self._key), "API key set" if self._key else "BRAVE_API_KEY missing")

    def search(self, query: str, *, kind: str = "web", max_results: int = 10, days: int | None = None,
               region: str = "IN", language: str = "en") -> list[SearchResult]:
        if not self._key:
            raise SearchError("config", self.name, "BRAVE_API_KEY is not set")
        params = {"q": query[:400], "count": max(1, min(20, max_results)), "country": region.lower(),
                  "search_lang": language, "safesearch": "moderate"}
        fresh = _freshness(days)
        if fresh:
            params["freshness"] = fresh
        path = "/news/search" if kind == "news" else "/web/search"
        response = request(self._client, self.name, "GET", BASE + path, self._timeout, params=params,
                           headers={"X-Subscription-Token": self._key, "Accept": "application/json"})
        try:
            data = response.json()
        except ValueError as exc:
            raise SearchError("bad_response", self.name) from exc
        items = data.get("results") if kind == "news" else (data.get("web") or {}).get("results")
        now = datetime.now(timezone.utc)
        out = []
        for i, item in enumerate(items or []):
            url = str(item.get("url") or "")
            if not url.startswith(("http://", "https://")):
                continue
            published = parse_date(item.get("page_age")) or relative_age(str(item.get("age") or ""), now)
            meta = item.get("meta_url") or {}
            out.append(SearchResult(title=str(item.get("title") or url)[:300], url=url,
                                    snippet=str(item.get("description") or "")[:1200], published=published,
                                    date_basis="publisher" if published else "unknown",
                                    source_name=str(meta.get("hostname") or ""), provider=self.name, rank=i))
        return out
