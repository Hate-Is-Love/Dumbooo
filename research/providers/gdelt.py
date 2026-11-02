"""GDELT DOC 2.0 API (keyless, https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/).

A second, independent news index used to compare coverage. Its dates are the time GDELT
*saw* the article ("news index"), which is close to — but not the same as — the
publisher's own date.
"""

from __future__ import annotations

from urllib.parse import quote

from research.dates import parse_date
from research.http import ClientFactory, default_client, request
from research.models import SearchResult
from research.search_provider import SearchError, SearchProvider

API = ("https://api.gdeltproject.org/api/v2/doc/doc?query={q}&mode=ArtList&maxrecords={n}&format=json"
       "&timespan={days}d&sort=DateDesc")


class GdeltProvider(SearchProvider):
    name = "gdelt"
    kinds = ("news",)

    def __init__(self, client_factory: ClientFactory = default_client, timeout: float = 25.0) -> None:
        self._client = client_factory
        self._timeout = timeout

    def search(self, query: str, *, kind: str = "news", max_results: int = 15, days: int | None = None,
               region: str = "IN", language: str = "en") -> list[SearchResult]:
        words = [w for w in query.replace('"', " ").split() if len(w) > 1][:8]
        if not words:
            return []
        q = " ".join(words) + (" sourcelang:english" if language == "en" else "")
        url = API.format(q=quote(q), n=max(1, min(50, max_results)), days=max(1, min(90, days or 7)))
        response = request(self._client, self.name, "GET", url, self._timeout)
        try:
            data = response.json()
        except ValueError as exc:
            # GDELT answers plain-text errors (e.g. "query too short") with HTTP 200
            raise SearchError("bad_response", self.name, response.text[:80]) from exc
        out = []
        for i, item in enumerate(data.get("articles") or []):
            url_ = str(item.get("url") or "")
            title = str(item.get("title") or "").strip()
            if not url_.startswith(("http://", "https://")) or len(title) < 15:
                continue
            published = parse_date(item.get("seendate"))
            out.append(SearchResult(title=title[:300], url=url_, snippet="", published=published,
                                    date_basis="news index" if published else "unknown",
                                    source_name=str(item.get("domain") or ""), provider=self.name, rank=i))
        return out
