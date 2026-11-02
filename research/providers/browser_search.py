"""Keyless web search through DUMBOO's own (headless) browser — the fallback when no search API key is set.

It loads the normal Bing results page in a separate, headless Edge profile and reads
the result links, exactly like a person would see them. This is less robust than an
API (the page layout can change), so every answer that used it says so, and the
README recommends a free Tavily key. It never signs in, solves CAPTCHAs or bypasses
anything: if Bing shows a challenge, the search is reported as blocked.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from research.dates import date_in_snippet
from research.models import SearchResult
from research.search_provider import SearchError, SearchProvider


def decode_bing(url: str) -> str:
    """Bing result links look like bing.com/ck/a?…&u=a1<base64url of the real URL>."""
    parts = urlsplit(url)
    if not parts.hostname or "bing.com" not in parts.hostname or not parts.path.startswith("/ck/"):
        return url
    value = (parse_qs(parts.query).get("u") or [""])[0]
    if value.startswith("a1"):
        value = value[2:]
        try:
            decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return url
        if decoded.startswith(("http://", "https://")):
            return decoded
    return url


class BrowserSearchProvider(SearchProvider):
    name = "browser"
    kinds = ("web",)

    def __init__(self, browser) -> None:
        self._browser = browser

    def search(self, query: str, *, kind: str = "web", max_results: int = 10, days: int | None = None,
               region: str = "IN", language: str = "en") -> list[SearchResult]:
        try:
            result = self._browser.search(query[:300])
        except Exception as exc:
            raise SearchError("unavailable", self.name, str(exc)[:120]) from exc
        if not result.get("ok"):
            error = result.get("error") or ""
            raise SearchError("network" if "internet" in error or "found" in error else "unavailable",
                              self.name, error[:120])
        items = result.get("results") or []
        if not items and ("challenge" in result.get("title", "").lower() or "captcha" in result.get("url", "")):
            raise SearchError("blocked", self.name, "the search page asked for a human check")
        now = datetime.now(timezone.utc)
        out = []
        seen = set()
        for i, item in enumerate(items[:max_results]):
            url = decode_bing(str(item.get("url") or ""))
            if not url.startswith(("http://", "https://")) or url in seen:
                continue
            seen.add(url)
            snippet = str(item.get("snippet") or "")
            published = date_in_snippet(snippet, now)
            out.append(SearchResult(title=str(item.get("title") or url)[:300], url=url, snippet=snippet[:800],
                                    published=published, date_basis="search engine date" if published else "unknown",
                                    provider=self.name, rank=i))
        return out
