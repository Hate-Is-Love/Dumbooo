"""Source quality, URL normalisation, de-duplication and independence of sources.

* Quality labels are about the *kind* of source (official/primary, established news,
  reference, community, low), not about whether a claim is true. Popularity or a
  high search rank is never treated as proof.
* Several sites repeating the same report are grouped together (same ``group``), so
  "five sources say so" is not mistaken for five independent confirmations.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from research.models import SearchResult, Source, domain_of

OFFICIAL_SUFFIXES = (".gov", ".gov.in", ".nic.in", ".gov.uk", ".gc.ca", ".gov.au", ".edu", ".ac.in", ".ac.uk",
                     ".edu.in", ".europa.eu", ".int", ".mil")
OFFICIAL_DOMAINS = {
    "python.org", "docs.python.org", "pypi.org", "developer.mozilla.org", "learn.microsoft.com", "docs.microsoft.com",
    "developer.apple.com", "developer.android.com", "developers.google.com", "cloud.google.com", "ai.google.dev",
    "platform.openai.com", "openai.com", "anthropic.com", "docs.anthropic.com", "kubernetes.io", "nodejs.org",
    "react.dev", "go.dev", "rust-lang.org", "doc.rust-lang.org", "oracle.com", "docs.oracle.com", "w3.org",
    "ietf.org", "rfc-editor.org", "arxiv.org", "nature.com", "science.org", "sciencedirect.com", "ieee.org",
    "acm.org", "pubmed.ncbi.nlm.nih.gov", "nih.gov", "who.int", "un.org", "worldbank.org", "imf.org", "rbi.org.in",
    "sebi.gov.in", "isro.gov.in", "nasa.gov", "esa.int", "readthedocs.io", "qt.io", "doc.qt.io",
    "pytorch.org", "tensorflow.org", "numpy.org", "pandas.pydata.org", "scikit-learn.org", "djangoproject.com",
    "flask.palletsprojects.com", "fastapi.tiangolo.com", "playwright.dev", "deepmind.google", "blog.google",
    "microsoft.com", "nvidia.com", "meta.com", "ai.meta.com", "cisa.gov", "nvd.nist.gov", "cve.org", "mitre.org",
}
NEWS_DOMAINS = {
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "theguardian.com", "nytimes.com", "washingtonpost.com",
    "ft.com", "bloomberg.com", "wsj.com", "economist.com", "npr.org", "aljazeera.com", "thehindu.com",
    "indianexpress.com", "hindustantimes.com", "timesofindia.indiatimes.com", "ndtv.com", "livemint.com",
    "economictimes.indiatimes.com", "business-standard.com", "thewire.in", "scroll.in", "techcrunch.com",
    "theverge.com", "arstechnica.com", "wired.com", "zdnet.com", "engadget.com", "cnbc.com", "cnn.com", "axios.com",
    "theregister.com", "bleepingcomputer.com", "securityweek.com", "therecord.media", "cybersecuritydive.com",
    "darkreading.com", "thehackernews.com", "krebsonsecurity.com", "space.com", "spacenews.com", "nature.com",
    "technologyreview.com", "venturebeat.com", "theinformation.com", "fortune.com", "forbes.com", "time.com",
    "abc.net.au", "dw.com", "france24.com", "politico.com", "semafor.com", "404media.co", "statnews.com",
}
REFERENCE_DOMAINS = {"wikipedia.org", "britannica.com", "stackoverflow.com", "realpython.com", "geeksforgeeks.org",
                     "w3schools.com", "freecodecamp.org", "khanacademy.org", "coursera.org", "ibm.com",
                     "investopedia.com", "mathworld.wolfram.com"}
COMMUNITY_DOMAINS = {"reddit.com", "medium.com", "dev.to", "quora.com", "news.ycombinator.com", "stackexchange.com",
                     "substack.com", "hashnode.dev", "linkedin.com"}
LOW_DOMAINS = {"pinterest.com", "facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com",
               "youtube.com", "scribd.com", "slideshare.net", "msn.com"}
LABEL_SCORE = {"official/primary": 1.0, "established news": 0.8, "reference": 0.65, "general": 0.5,
               "community": 0.35, "low": 0.15}
TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_src|cmpid|ocid|oc$|ito$|s_cid|guccounter)", re.I)


def canonical_url(url: str) -> str:
    """Same page, same key: lower-case host without www, no tracking parameters, no fragment."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING.match(k)])
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, query, ""))


def _matches(domain: str, names: set[str]) -> bool:
    return any(domain == n or domain.endswith("." + n) for n in names)


SECOND_LEVEL = {"co", "com", "gov", "ac", "edu", "org", "net", "nic", "res"}


def base_domain(domain: str) -> str:
    """docs.python.org -> python.org; www.bbc.co.uk -> bbc.co.uk (one organisation, one origin)."""
    parts = domain.split(".")
    if len(parts) >= 3 and parts[-2] in SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _github_label(url: str, query: str) -> str:
    """A GitHub repository is primary only for a query naming its owner or project."""
    path = [p.lower() for p in urlsplit(url).path.split("/") if p][:2]
    q = {w for w in re.findall(r"[a-z0-9][a-z0-9_.-]{1,}", query.lower())}
    return "official/primary" if path and (set(path) & q) else "community"


def quality_label(url: str, query: str = "") -> str:
    domain = domain_of(url)
    if domain == "github.com":
        return _github_label(url, query)
    if domain.endswith(OFFICIAL_SUFFIXES) or _matches(domain, OFFICIAL_DOMAINS):
        return "official/primary"
    words = [w for w in re.findall(r"[a-z0-9]{3,}", query.lower()) if w not in ("the", "and", "for", "official",
                                                                               "documentation", "docs", "latest")]
    # A product's own site for a query naming it: pandas.pydata.org for "pandas", docs.flutter.dev for "flutter"
    labels = domain.split(".")[:-1]
    if labels and words and any(w in labels for w in words) and (
            labels[0] in words or labels[0] in ("docs", "doc", "developer", "developers", "api", "learn")):
        return "official/primary"
    if _matches(domain, NEWS_DOMAINS):
        return "established news"
    if _matches(domain, REFERENCE_DOMAINS):
        return "reference"
    if _matches(domain, COMMUNITY_DOMAINS):
        return "community"
    if _matches(domain, LOW_DOMAINS):
        return "low"
    return "general"


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower())}


def title_similarity(a: str, b: str) -> float:
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def dedupe(results: list[SearchResult]) -> list[SearchResult]:
    """Drop repeated URLs (after normalisation); keep the earliest rank and the best date."""
    seen: dict[str, SearchResult] = {}
    for r in results:
        key = canonical_url(r.url)
        existing = seen.get(key)
        if existing is None:
            seen[key] = r
        elif existing.published is None and r.published is not None:
            existing.published, existing.date_basis = r.published, r.date_basis
    return list(seen.values())


def score(result: SearchResult, query: str, *, news: bool, now: datetime | None = None) -> float:
    """Relevance (provider rank, words in the title), freshness (news) and kind of source."""
    now = now or datetime.now(timezone.utc)
    relevance = 1.0 / (1 + result.rank * 0.25)
    overlap = len(words(query) & words(result.title + " " + result.snippet)) / max(1, len(words(query)))
    label = quality_label(result.quality_url, query)
    s = 0.45 * relevance + 0.25 * overlap + 0.30 * LABEL_SCORE[label]
    if news:
        if result.published is None:
            s -= 0.15
        else:
            age_days = max(0.0, (now - result.published).total_seconds() / 86400)
            s += 0.25 / (1 + age_days / 2)
    return s


def rank(results: list[SearchResult], query: str, *, news: bool = False) -> list[SearchResult]:
    return sorted(dedupe(results), key=lambda r: score(r, query, news=news), reverse=True)


def assign_groups(sources: list[Source]) -> int:
    """Group sources that repeat one original report (very similar headline or the same site).

    Returns the number of independent groups.
    """
    groups: list[list[Source]] = []
    for s in sources:
        placed = False
        for g in groups:
            head = g[0]
            if base_domain(s.domain) == base_domain(head.domain) or title_similarity(s.title, head.title) >= 0.6:
                g.append(s)
                placed = True
                break
        if not placed:
            groups.append([s])
    for n, g in enumerate(groups, 1):
        for s in g:
            s.group = n
    return len(groups)


def independent_count(sources: list[Source]) -> int:
    return len({s.group or s.id for s in sources})
