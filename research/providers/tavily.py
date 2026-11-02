"""Tavily Search API (https://docs.tavily.com) — built for AI research, free plan of 1,000 searches/month.

Needs ``TAVILY_API_KEY``. The key is sent as a Bearer header.
"""

from __future__ import annotations

from research.dates import parse_date
from research.http import ClientFactory, default_client, request
from research.models import SearchResult
from research.search_provider import ProviderStatus, SearchError, SearchProvider

ENDPOINT = "https://api.tavily.com/search"


class TavilyProvider(SearchProvider):
    name = "tavily"
    kinds = ("web", "news")
    needs_key = True

    def __init__(self, api_key: str, client_factory: ClientFactory = default_client, timeout: float = 20.0) -> None:
        self._key = api_key
        self._client = client_factory
        self._timeout = timeout

    def status(self) -> ProviderStatus:
        return ProviderStatus(self.name, bool(self._key), "API key set" if self._key else "TAVILY_API_KEY missing")

    def search(self, query: str, *, kind: str = "web", max_results: int = 10, days: int | None = None,
               region: str = "IN", language: str = "en") -> list[SearchResult]:
        if not self._key:
            raise SearchError("config", self.name, "TAVILY_API_KEY is not set")
        body = {"query": query[:400], "topic": "news" if kind == "news" else "general",
                "search_depth": "basic", "max_results": max(1, min(20, max_results)), "include_answer": False}
        if kind == "news" and days:
            body["days"] = int(days)
        response = request(self._client, self.name, "POST", ENDPOINT, self._timeout, json=body,
                           headers={"Authorization": f"Bearer {self._key}"})
        try:
            items = response.json().get("results") or []
        except ValueError as exc:
            raise SearchError("bad_response", self.name) from exc
        out = []
        for i, item in enumerate(items):
            url = str(item.get("url") or "")
            if not url.startswith(("http://", "https://")):
                continue
            published = parse_date(item.get("published_date"))
            out.append(SearchResult(title=str(item.get("title") or url)[:300], url=url,
                                    snippet=str(item.get("content") or "")[:1200], published=published,
                                    date_basis="publisher" if published else "unknown", provider=self.name,
                                    rank=i))
        return out
