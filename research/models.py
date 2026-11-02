"""Data structures shared by the research services."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


@dataclass
class SearchResult:
    """One hit from a search provider. ``snippet`` is the provider's text, not verified on the page."""

    title: str
    url: str
    snippet: str = ""
    published: datetime | None = None      # aware UTC datetime when the provider gives one
    date_basis: str = "unknown"            # publisher | news index | declared by the page | search engine date | unknown
    source_name: str = ""                  # e.g. "Reuters" (news feeds)
    provider: str = ""
    rank: int = 0
    site_url: str = ""                     # publisher's site when ``url`` is an aggregator link (Google News)

    @property
    def quality_url(self) -> str:
        return self.site_url or self.url


@dataclass
class Source:
    """A numbered, citable source. Only sources DUMBOO actually received can be cited."""

    id: int
    title: str
    url: str
    snippet: str = ""
    published: datetime | None = None
    date_basis: str = "unknown"
    source_name: str = ""
    provider: str = ""
    read: bool = False                     # True when the page itself was fetched and its text extracted
    read_error: str = ""
    text: str = ""                         # extracted page text (kept in memory only, never stored in full)
    passages: list[str] = field(default_factory=list)
    retrieved_at: datetime | None = None
    quality: float = 0.5
    quality_label: str = "general"
    group: int = 0                         # sources with the same group repeat the same original report
    original_url: str = ""                 # e.g. the Google News link before it was resolved

    @property
    def domain(self) -> str:
        return domain_of(self.url)

    def published_text(self) -> str:
        if self.published is None:
            return "date unknown"
        return self.published.astimezone().strftime("%d %b %Y")

    def stored(self) -> dict[str, Any]:
        """What is saved in research history: metadata and a short extract, never the whole article."""
        return {"id": self.id, "title": self.title[:300], "url": self.url, "domain": self.domain,
                "snippet": self.snippet[:400], "published": self.published.isoformat() if self.published else None,
                "date_basis": self.date_basis, "source_name": self.source_name[:80], "provider": self.provider,
                "read": self.read, "read_error": self.read_error[:200],
                "retrieved_at": self.retrieved_at.isoformat() if self.retrieved_at else None,
                "quality_label": self.quality_label, "group": self.group,
                "extract": (self.passages[0] if self.passages else "")[:500]}


@dataclass
class Finding:
    text: str
    source_ids: list[int] = field(default_factory=list)
    kind: str = "fact"                     # fact | claim | analysis | uncertain
    headline: str = ""
    published: str = ""
    why: str = ""
    uncertainty: str = ""
    verdict: str = ""                      # fact-check: supported | contradicted | partly_supported | insufficient | outdated
    contradicting_ids: list[int] = field(default_factory=list)
    independent_sources: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ResearchResult:
    kind: str                              # search | news | research | factcheck | open_docs
    query: str
    status: str = "running"                # running | completed | partial | failed | cancelled
    summary: str = ""
    findings: list[Finding] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    comparison: str = ""
    uncertainty: str = ""
    gaps: list[str] = field(default_factory=list)
    period: str = ""                       # news: the time window searched
    notes: list[str] = field(default_factory=list)   # limitations (fallback provider, unreadable pages …)
    error: str = ""
    ai_used: bool = False
    memory_used: list[str] = field(default_factory=list)
    previous: dict | None = None           # earlier research shown for comparison (clearly labelled)
    plan: list[dict] = field(default_factory=list)
    id: int | None = None
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    searches: int = 0
    pages: int = 0

    def source(self, source_id: int) -> Source | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def cited_ids(self) -> set[int]:
        ids: set[int] = set()
        for f in self.findings:
            ids.update(f.source_ids)
            ids.update(f.contradicting_ids)
        return ids
