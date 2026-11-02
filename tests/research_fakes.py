"""Offline stand-ins for research: scripted search providers and a fake web (httpx.MockTransport)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx

from research.models import SearchResult
from research.search_provider import SearchError, SearchProvider


def ago(days: float = 0, hours: float = 0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days, hours=hours)


class FakeSearchProvider(SearchProvider):
    """Returns scripted results per kind; can raise scripted errors (one per call, in order)."""

    def __init__(self, name: str, results: dict[str, list[SearchResult]] | None = None, kinds=("web", "news"),
                 errors: list[SearchError] | None = None, needs_key: bool = False) -> None:
        self.name = name
        self.kinds = kinds
        self.results = results or {}
        self.errors = list(errors or [])
        self.needs_key = needs_key
        self.calls: list[tuple[str, str, int | None]] = []

    def search(self, query: str, *, kind: str = "web", max_results: int = 10, days: int | None = None,
               region: str = "IN", language: str = "en") -> list[SearchResult]:
        self.calls.append((query, kind, days))
        if self.errors:
            raise self.errors.pop(0)
        return [SearchResult(r.title, r.url, r.snippet, r.published, r.date_basis, r.source_name, self.name, i,
                             r.site_url) for i, r in enumerate(self.results.get(kind, [])[:max_results])]


class FakeWeb:
    """A tiny fake internet: url -> (status, content-type, body). Unknown pages answer 404."""

    def __init__(self) -> None:
        self.pages: dict[str, tuple[int, str, bytes]] = {}
        self.requests: list[str] = []

    def add(self, url: str, html: str, status: int = 200, ctype: str = "text/html; charset=utf-8") -> None:
        self.pages[url] = (status, ctype, html.encode("utf-8"))

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        status, ctype, body = self.pages.get(url, (404, "text/plain", b"not found"))
        return httpx.Response(status, headers={"content-type": ctype}, content=body, request=request)

    def client_factory(self) -> Callable[..., httpx.Client]:
        def factory(timeout: float = 15.0, **kw) -> httpx.Client:
            kw.pop("follow_redirects", None)
            return httpx.Client(transport=httpx.MockTransport(self.handler), timeout=timeout,
                                follow_redirects=False, **kw)
        return factory


def offline_client() -> Callable[..., httpx.Client]:
    return FakeWeb().client_factory()


def fake_resolver(host: str, port=None):
    """Every test host resolves to a public documentation address; 'localhost'/'internal' are private."""
    if host in ("localhost", "internal.example") or host.startswith(("127.", "10.", "192.168.")):
        return [(2, 1, 6, "", ("127.0.0.1" if host == "localhost" else "192.168.1.10", 0))]
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


def article(title: str, body: str, date: str = "") -> str:
    meta = f'<meta property="article:published_time" content="{date}">' if date else ""
    paragraphs = "".join(f"<p>{p}</p>" for p in body.split("\n\n"))
    return (f"<html><head><title>{title}</title>{meta}</head><body><article><h1>{title}</h1>{paragraphs}"
            f"</article></body></html>")


class RoutingAI:
    """A fake AI provider for research: answers by prompt type (planner, search, news, report, claims, verdicts)."""

    def __init__(self, **answers) -> None:
        from tests.ai_fakes import ScriptedProvider  # noqa: F401  (same interface)
        self.answers = answers          # kind -> str or callable(request) -> str
        self.requests = []
        self.errors: list = []
        self.name, self.model = "fake", "fake-model"

    @staticmethod
    def kind_of(system: str) -> str:
        if "You plan web research" in system:
            return "plan"
        if "Extract the separate factual claims" in system:
            return "claims"
        if '"verdict"' in system:
            return "verdict"
        if '"developments"' in system:
            return "news"
        if '"executive_summary"' in system:
            return "report"
        if '"answer"' in system:
            return "search"
        return "chat"

    def _reply(self, request):
        import json as _json

        from ai.base import AIResult

        self.requests.append(request)
        if self.errors:
            raise self.errors.pop(0)
        kind = self.kind_of(request.system_prompt)
        value = self.answers.get(kind, "{}" if kind != "chat" else "An ordinary chat answer.")
        value = value(request) if callable(value) else value
        return AIResult(value if isinstance(value, str) else _json.dumps(value), model=self.model)

    def generate_response(self, request, cancel=None):
        return self._reply(request)

    def stream_response(self, request, cancel=None):
        from ai.base import StreamChunk

        yield StreamChunk(text=self._reply(request).text)
        yield StreamChunk(finish_reason="stop")

    def health_check(self):
        from ai.base import HealthStatus
        return HealthStatus(True, "ok")

    def cancel_generation(self):
        pass

    @property
    def supports_streaming(self):
        return True

    def describe(self):
        return "fake"

    def of(self, kind: str):
        return [r for r in self.requests if self.kind_of(r.system_prompt) == kind]
