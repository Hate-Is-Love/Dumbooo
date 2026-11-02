"""Opening search results and extracting their text — safely.

* Only public ``http(s)`` addresses: every host (including each redirect) is resolved
  and refused if it points to this computer, the local network or other private
  ranges, so a web page can't make DUMBOO probe your router or local services.
* ``robots.txt`` is respected (configurable). Pages that answer "access denied",
  require a login, or are paywalled are reported as not accessible — never bypassed.
* Size and time limits; HTML (main text via trafilatura), PDF (pypdf) and plain text.
* Google News and Bing links are resolved to the real article first.
* Pages that are empty without JavaScript are rendered in DUMBOO's headless browser
  (the same as a normal visit; not used for pages that refused access).
* Page content is untrusted data: it is only ever *quoted* to the AI.
"""

from __future__ import annotations

import io
import ipaddress
import logging
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from research.config import ResearchConfig
from research.dates import declared_date, url_date
from research.evidence_extraction_service import best_passages
from research.http import USER_AGENT, default_client
from research.models import Source, utcnow
from research.providers.browser_search import decode_bing

log = logging.getLogger(__name__)

ROBOTS_AGENT = "DUMBOO-Research"
PAGE_CACHE_TTL = 1800.0
MIN_USEFUL_TEXT = 400


class FetchError(RuntimeError):
    pass


def _public_host(host: str, resolver: Callable = socket.getaddrinfo) -> bool:
    if not host:
        return False
    try:
        infos = resolver(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            return False
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(infos)


class SourceRetrievalService:
    def __init__(self, config: Callable[[], ResearchConfig], browser=None,
                 client_factory: Callable[..., httpx.Client] = default_client,
                 resolver: Callable = socket.getaddrinfo, clock: Callable[[], float] = time.monotonic) -> None:
        self._config = config
        self._browser = browser
        self._client = client_factory
        self._resolver = resolver
        self._clock = clock
        self._robots: dict[str, RobotFileParser | None] = {}
        self._pages: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self.fetches = 0

    # -- safety checks -----------------------------------------------------------------------------
    def check_url(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise FetchError("only http(s) pages can be opened")
        if parts.username or parts.password:
            raise FetchError("addresses with user names or passwords are not opened")
        if not _public_host(parts.hostname or "", self._resolver):
            raise FetchError("the address is not a public internet site (local or private addresses are blocked)")

    def _allowed_by_robots(self, url: str) -> bool:
        if not self._config().respect_robots:
            return True
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            known = base in self._robots
            parser = self._robots.get(base)
        if not known:
            parser = None
            try:
                with self._client(timeout=6.0) as client:
                    r = client.get(base + "/robots.txt")
                if r.status_code == 200 and len(r.content) < 500_000:
                    parser = RobotFileParser()
                    parser.parse(r.text.splitlines())
            except httpx.HTTPError:
                parser = None  # robots.txt unreachable: treated as no restrictions (standard behaviour)
            with self._lock:
                self._robots[base] = parser
        return parser is None or parser.can_fetch(ROBOTS_AGENT, url)

    # -- fetching ----------------------------------------------------------------------------------
    def _fetch(self, url: str, cancel: threading.Event | None) -> dict:
        cfg = self._config()
        limit = cfg.max_page_kb * 1024
        current = url
        with self._client(timeout=cfg.page_timeout_s, follow_redirects=False) as client:
            for _hop in range(6):
                self.check_url(current)
                if cancel is not None and cancel.is_set():
                    raise FetchError("cancelled")
                try:
                    with client.stream("GET", current, headers={"User-Agent": USER_AGENT,
                                                                "Accept": "text/html,application/pdf,text/plain;q=0.9,*/*;q=0.5"}) as r:
                        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                            current = urljoin(current, r.headers["location"])
                            continue
                        if r.status_code in (401, 402, 403, 451):
                            raise FetchError(f"the site refused access (HTTP {r.status_code}; login, paywall or "
                                             "blocking) — not bypassed")
                        if r.status_code == 404:
                            raise FetchError("the page no longer exists (HTTP 404)")
                        if r.status_code == 429:
                            raise FetchError("the site is rate-limiting requests (HTTP 429)")
                        if r.status_code >= 400:
                            raise FetchError(f"the site answered with an error (HTTP {r.status_code})")
                        ctype = r.headers.get("content-type", "").lower()
                        chunks, size = [], 0
                        for chunk in r.iter_bytes():
                            size += len(chunk)
                            if size > limit:
                                raise FetchError(f"the page is larger than {cfg.max_page_kb} KB")
                            if cancel is not None and cancel.is_set():
                                raise FetchError("cancelled")
                            chunks.append(chunk)
                        return {"url": str(r.url), "ctype": ctype, "body": b"".join(chunks), "status": r.status_code,
                                "encoding": r.encoding or "utf-8"}
                except httpx.TimeoutException as exc:
                    raise FetchError(f"the page did not load within {cfg.page_timeout_s:.0f} s") from exc
                except httpx.HTTPError as exc:
                    raise FetchError(f"the page could not be loaded ({exc.__class__.__name__})") from exc
        raise FetchError("too many redirects")

    @staticmethod
    def _extract_html(html: str) -> tuple[str, str, datetime | None]:
        """Main text, title and the publication date the page *declares* (never a guessed one)."""
        import re as _re

        import trafilatura

        text = trafilatura.extract(html, include_comments=False, include_tables=True, favor_precision=True) or ""
        m = _re.search(r"<title[^>]*>(.*?)</title>", html[:200_000], _re.I | _re.S)
        title = " ".join(m.group(1).split())[:300] if m else ""
        return text, title, declared_date(html)

    @staticmethod
    def _extract_pdf(body: bytes) -> str:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(body))
        out = []
        for page in reader.pages[:40]:
            out.append(page.extract_text() or "")
            if sum(len(p) for p in out) > 150_000:
                break
        return "\n\n".join(out)

    def read(self, url: str, cancel: threading.Event | None = None) -> dict:
        """``{"url", "title", "text", "date"}`` for one page; raises ``FetchError`` with a readable reason."""
        now = self._clock()
        with self._lock:
            hit = self._pages.get(url)
            if hit and now - hit[0] < PAGE_CACHE_TTL:
                return dict(hit[1])
        self.check_url(url)
        if not self._allowed_by_robots(url):
            raise FetchError("the site's robots.txt asks automated readers not to open this page")
        self.fetches += 1
        page = self._fetch(url, cancel)
        ctype, body = page["ctype"], page["body"]
        title, date = "", None
        if "pdf" in ctype or body[:5] == b"%PDF-":
            text = self._extract_pdf(body)
        elif "html" in ctype or "xml" in ctype or not ctype:
            html = body.decode(page["encoding"], errors="replace")
            text, title, date = self._extract_html(html)
            if len(text) < MIN_USEFUL_TEXT and self._browser is not None:
                try:  # a JavaScript-only page: render it like a normal browser visit
                    rendered = self._browser.read_rendered(page["url"], self._config().page_timeout_s)
                    if (rendered.get("status") or 200) < 400:
                        text2, title2, date2 = self._extract_html(rendered.get("html") or "")
                        if len(text2) > len(text):
                            text, title, date = text2, title2 or rendered.get("title", ""), date2 or date
                except Exception:
                    log.debug("Rendering %s failed", page["url"], exc_info=True)
        elif ctype.startswith("text/"):
            text = body.decode(page["encoding"], errors="replace")
        else:
            raise FetchError(f"unsupported content type ({ctype.split(';')[0] or 'unknown'})")
        if len(text.strip()) < 80:
            raise FetchError("no readable text on the page")
        result = {"url": page["url"], "title": title, "text": text[:200_000], "date": date}
        with self._lock:
            self._pages[url] = (self._clock(), result)
        return result

    def resolve(self, source: Source, timeout_s: float = 15.0) -> None:
        """Replace aggregator links (Google News, Bing) with the publisher's own URL."""
        url = decode_bing(source.url)
        host = (urlsplit(url).hostname or "").lower()
        if host == "news.google.com":
            if self._browser is None:
                raise FetchError("Google News links need DUMBOO's browser to open")
            resolved = self._browser.resolve(url, "news.google.com", timeout_s)
            if "news.google.com" in (urlsplit(resolved).hostname or "") or not resolved.startswith("http"):
                raise FetchError("the Google News link could not be opened")
            url = resolved
        if url != source.url:
            source.original_url = source.url
            source.url = url

    def retrieve(self, source: Source, question: str, cancel: threading.Event | None = None) -> Source:
        """Fill ``source`` with the page's text and the passages relevant to ``question``."""
        source.retrieved_at = utcnow()
        try:
            self.resolve(source, self._config().page_timeout_s)
            page = self.read(source.url, cancel)
        except FetchError as exc:
            source.read, source.read_error = False, str(exc)
            return source
        except Exception as exc:  # never let one bad page break the research
            log.debug("Reading %s failed", source.url, exc_info=True)
            source.read, source.read_error = False, f"the page could not be read ({exc.__class__.__name__})"
            return source
        source.read = True
        source.text = page["text"]
        if page["url"] and page["url"] != source.url:
            source.original_url = source.original_url or source.url
            source.url = page["url"]
        if source.published is None and page.get("date") is not None:
            source.published, source.date_basis = page["date"], "declared by the page"
        elif source.published is None and url_date(source.url) is not None:
            source.published, source.date_basis = url_date(source.url), "from the page address"
        if not source.title or source.title == source.url:
            source.title = page.get("title") or source.title
        source.passages = best_passages(page["text"], question)
        return source
