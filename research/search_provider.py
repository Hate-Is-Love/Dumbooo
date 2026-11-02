"""The search-provider interface. Providers are interchangeable behind it.

Every provider raises ``SearchError`` with a ``kind`` the rest of DUMBOO
understands (config, auth, rate_limit, quota, timeout, network, unavailable,
blocked, empty), so failures are reported honestly instead of pretending a
search succeeded.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from research.models import SearchResult

ERROR_TEXT = {
    "config": "the search provider is not configured",
    "auth": "the search provider rejected the API key",
    "rate_limit": "the search provider is rate-limiting requests (too many in a short time)",
    "quota": "the search provider's usage quota is used up",
    "timeout": "the search timed out",
    "network": "there is no connection to the search provider (check the internet connection)",
    "unavailable": "the search provider is temporarily unavailable",
    "blocked": "the search request was blocked",
    "bad_response": "the search provider returned an unreadable answer",
}


class SearchError(RuntimeError):
    def __init__(self, kind: str, provider: str, detail: str = "", retry_after: float | None = None) -> None:
        self.kind = kind
        self.provider = provider
        self.detail = detail
        self.retry_after = retry_after
        super().__init__(f"{provider}: {kind} {detail}".strip())

    @property
    def user_message(self) -> str:
        detail = f": {self.detail}" if self.kind == "config" and self.detail else ""
        return f"{ERROR_TEXT.get(self.kind, 'the search failed')} ({self.provider}{detail})"

    @property
    def retryable(self) -> bool:
        return self.kind in ("timeout", "network", "unavailable", "rate_limit")


@dataclass
class ProviderStatus:
    name: str
    configured: bool
    detail: str


class SearchProvider(ABC):
    name = "base"
    kinds: tuple[str, ...] = ("web",)      # "web" and/or "news"
    needs_key = False

    @abstractmethod
    def search(self, query: str, *, kind: str = "web", max_results: int = 10, days: int | None = None,
               region: str = "IN", language: str = "en") -> list[SearchResult]:
        """Run one search. ``days`` limits results to the last N days when the provider supports it."""

    def status(self) -> ProviderStatus:
        return ProviderStatus(self.name, True, "ready")

    def close(self) -> None:
        pass
