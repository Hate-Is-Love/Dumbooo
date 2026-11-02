"""Parsing publication dates from providers, feeds and search snippets (always to aware UTC)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"


def to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_date(value: str | None) -> datetime | None:
    """ISO 8601, RFC 2822 (RSS) or GDELT's ``20261021T184500Z``. ``None`` if unparseable."""
    if not value:
        return None
    text = str(value).strip()
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z", text)
    if m:
        return datetime(*map(int, m.groups()), tzinfo=timezone.utc)
    try:
        return to_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        pass
    try:
        return to_utc(parsedate_to_datetime(text))
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%b %d, %Y", "%B %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def relative_age(text: str, now: datetime) -> datetime | None:
    """'3 hours ago', '2 days ago', 'yesterday' (search snippets and Brave's 'age')."""
    t = text.strip().lower()
    if t.startswith("yesterday"):
        return now - timedelta(days=1)
    m = re.match(r"(\d+)\s*(minute|min|hour|hr|day|week|month|year)s?\s+ago", t)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    days = {"minute": n / 1440, "min": n / 1440, "hour": n / 24, "hr": n / 24, "day": n, "week": 7 * n,
            "month": 30 * n, "year": 365 * n}[unit]
    return now - timedelta(days=days)


def date_in_snippet(snippet: str, now: datetime) -> datetime | None:
    """A date at the start of a search snippet: 'Oct 21, 2026 · …' or '3 days ago · …'."""
    head = re.split(r"\s[·—-]\s", snippet.strip(), maxsplit=1)[0][:40]
    rel = relative_age(head, now)
    if rel is not None:
        return rel
    m = re.match(rf"((?:{_MONTHS})[a-z]* \d{{1,2}}, \d{{4}}|\d{{1,2}} (?:{_MONTHS})[a-z]* \d{{4}})", head, re.I)
    return parse_date(m.group(1).replace("Sept", "Sep")) if m else None


_META_DATE = re.compile(
    r"<meta[^>]+(?:property|name|itemprop)=[\"'](?:article:published_time|og:article:published_time|datePublished|"
    r"publish-date|publishdate|pubdate|dc\.date\.issued|dc\.date|parsely-pub-date|sailthru\.date|date)[\"'][^>]*>",
    re.I)
_CONTENT = re.compile(r"content=[\"']([^\"']{8,40})[\"']", re.I)
_JSONLD_DATE = re.compile(r"\"datePublished\"\s*:\s*\"([^\"]{8,40})\"")
_TIME_TAG = re.compile(r"<time[^>]+datetime=[\"']([^\"']{8,40})[\"'][^>]*(?:pubdate|itemprop=[\"']datePublished)", re.I)


def declared_date(html: str) -> datetime | None:
    """The publication date a page *explicitly declares* in its markup; ``None`` instead of guessing."""
    head = html[:400_000]
    for tag in _META_DATE.findall(head):
        m = _CONTENT.search(tag)
        if m:
            dt = parse_date(m.group(1))
            if dt:
                return dt
    for pattern in (_JSONLD_DATE, _TIME_TAG):
        m = pattern.search(head)
        if m:
            dt = parse_date(m.group(1))
            if dt:
                return dt
    return None


def url_date(url: str) -> datetime | None:
    """A date that is part of the page address: /2022/10/24/… or /2022/10/…"""
    m = re.search(r"/(20\d{2}|19\d{2})/(0[1-9]|1[0-2])(?:/(0[1-9]|[12]\d|3[01]))?/", url)
    if not m:
        return None
    return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1), tzinfo=timezone.utc)


def window_label(days: int, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).astimezone()
    end = now.astimezone()
    return f"{start:%d %b %Y} – {end:%d %b %Y} (last {days} day{'s' if days != 1 else ''})"
