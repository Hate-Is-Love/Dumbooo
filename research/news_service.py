"""Latest-news searches with date checking.

* The time window comes from the request ("today" = 1 day, "this week" = 7, "this
  month" = 31, default ``research.news_days``) and is always stated in the answer.
* Every article's publication date is checked: articles older than the window are
  dropped (and counted), undated articles are kept but never called "latest".
* Articles about the same story from different publishers are grouped, so a story's
  coverage can be compared and syndicated copies aren't counted as independent.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from research.dates import window_label
from research.models import SearchResult, domain_of
from research.quality import LABEL_SCORE, quality_label, title_similarity, words
from research.web_search_service import SearchOutcome, WebSearchService

# Press-release wires and similar pages are not news reporting.
LOW_QUALITY_NEWS = re.compile(r"(newswiretoday|prnewswire|businesswire|globenewswire|einpresswire|openpr|"
                              r"accesswire|newsfilecorp|prweb|issuewire)\.", re.I)
PRESS_RELEASE = re.compile(r"\b(press release|sponsored|advertorial)\b", re.I)


def window_days(text: str, default: int) -> int:
    t = text.lower()
    if re.search(r"\b(today|today's|todays|this morning|tonight|right now|breaking)\b", t):
        return 1
    if re.search(r"\b(yesterday)\b", t):
        return 2
    if re.search(r"\b(this|past|last) week\b|\bweekly\b", t):
        return 7
    if re.search(r"\b(this|past|last) month\b", t):
        return 31
    if re.search(r"\b(this|past|last) year\b", t):
        return 365
    m = re.search(r"\b(?:past|last) (\d{1,2}) days?\b", t)
    if m:
        return max(1, min(60, int(m.group(1))))
    return default


@dataclass
class Story:
    articles: list[SearchResult]

    @property
    def lead(self) -> SearchResult:
        return self.articles[0]

    @property
    def newest(self) -> datetime | None:
        dates = [a.published for a in self.articles if a.published]
        return max(dates) if dates else None


@dataclass
class NewsOutcome:
    stories: list[Story]
    days: int
    period: str
    providers: list[str]
    dropped_old: int = 0
    undated: int = 0
    notes: list[str] = field(default_factory=list)
    errors: list = field(default_factory=list)


class NewsService:
    def __init__(self, search: WebSearchService) -> None:
        self._search = search

    def latest(self, query: str, days: int, *, now: datetime | None = None, max_stories: int = 6,
               cancel: threading.Event | None = None, fresh: bool = False) -> NewsOutcome:
        now = now or datetime.now(timezone.utc)
        outcome: SearchOutcome = self._search.search(query, "news", max_results=25, days=days, cancel=cancel,
                                                     fresh=fresh)
        cutoff = now - timedelta(days=days, hours=6)  # small margin for time zones
        kept, old, undated = [], 0, 0
        topic_words = words(query) - {"news", "latest", "top", "today", "recent", "update", "updates"}
        for r in outcome.results:
            if r.provider == "gdelt" and topic_words and not topic_words & words(r.title):
                continue  # GDELT matches anywhere in the article; require the topic in the headline
            if LOW_QUALITY_NEWS.search(r.quality_url) or PRESS_RELEASE.search(r.title):
                continue
            if r.published is None:
                undated += 1
                kept.append(r)
            elif r.published < cutoff:
                old += 1
            elif r.published > now + timedelta(hours=12):
                old += 1  # a future date is a data error, not news
            else:
                kept.append(r)
        stories = self.group(kept)
        stories.sort(key=lambda s: self.importance(s, now, days), reverse=True)
        notes = list(outcome.notes)
        if old:
            notes.append(f"{old} older article(s) outside the period were left out.")
        if undated:
            notes.append(f"{undated} article(s) had no publication date; they are marked 'date unknown'.")
        return NewsOutcome(stories[:max_stories], days, window_label(days, now), outcome.providers, old, undated,
                           notes, outcome.errors)

    @staticmethod
    def importance(story: Story, now: datetime, days: int) -> float:
        """Coverage by several publishers, the kind of source, curated feeds, then recency."""
        publishers = len({a.source_name or domain_of(a.quality_url) for a in story.articles})
        coverage = min(publishers, 4) / 4
        quality = LABEL_SCORE[quality_label(story.lead.quality_url)]
        curated = 0.15 if any(a.provider != "gdelt" for a in story.articles) else 0.0
        if story.newest is None:
            recency = 0.0
        else:
            age = max(0.0, (now - story.newest).total_seconds() / 86400)
            recency = max(0.0, 1 - age / max(1, days))
        return 0.40 * coverage + 0.30 * quality + curated + 0.15 * recency

    @staticmethod
    def group(results: list[SearchResult]) -> list[Story]:
        """Articles with very similar headlines are the same story (from different publishers)."""
        stories: list[Story] = []
        for r in results:
            for story in stories:
                if title_similarity(r.title, story.lead.title) >= 0.45:
                    story.articles.append(r)
                    break
            else:
                stories.append(Story([r]))
        for story in stories:  # the most authoritative, then the newest, article leads each story
            story.articles.sort(key=lambda a: ({"official/primary": 2, "established news": 1}.get(
                quality_label(a.quality_url), 0), a.published or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
        return stories
