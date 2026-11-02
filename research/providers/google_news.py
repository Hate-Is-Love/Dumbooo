"""Google News RSS search (keyless): headlines with publication time and publisher.

Feed: ``https://news.google.com/rss/search?q=…+when:7d`` (documented RSS output of
Google News). Links point to Google News and are resolved to the original article
by the source-retrieval service before a page is read.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

from research.dates import parse_date
from research.http import ClientFactory, default_client, request
from research.models import SearchResult
from research.search_provider import SearchError, SearchProvider

FEED = "https://news.google.com/rss/search?q={q}&hl={lang}-{region}&gl={region}&ceid={region}:{lang}"
MAX_FEED_BYTES = 3_000_000


class GoogleNewsProvider(SearchProvider):
    name = "google_news"
    kinds = ("news",)

    def __init__(self, client_factory: ClientFactory = default_client, timeout: float = 15.0) -> None:
        self._client = client_factory
        self._timeout = timeout

    def search(self, query: str, *, kind: str = "news", max_results: int = 15, days: int | None = None,
               region: str = "IN", language: str = "en") -> list[SearchResult]:
        q = query[:300] + (f" when:{int(days)}d" if days else "")
        url = FEED.format(q=quote_plus(q), lang=language, region=region.upper())
        response = request(self._client, self.name, "GET", url, self._timeout)
        if len(response.content) > MAX_FEED_BYTES:
            raise SearchError("bad_response", self.name, "feed too large")
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise SearchError("bad_response", self.name, "unreadable feed") from exc
        out = []
        for i, item in enumerate(root.findall("./channel/item")[:max_results]):
            link = (item.findtext("link") or "").strip()
            if not link.startswith("https://"):
                continue
            source = item.find("source")
            publisher = (source.text or "").strip() if source is not None else ""
            publisher_url = source.get("url", "") if source is not None else ""
            title = (item.findtext("title") or "").strip()
            if publisher and title.endswith(" - " + publisher):
                title = title[: -len(publisher) - 3]
            description = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
            published = parse_date(item.findtext("pubDate"))
            out.append(SearchResult(title=title[:300], url=link, snippet=" ".join(description.split())[:500],
                                    published=published, date_basis="publisher" if published else "unknown",
                                    source_name=publisher or publisher_url, provider=self.name, rank=i,
                                    site_url=publisher_url))
        return out
