"""DUMBOO's own browser window, driven with Playwright.

* Uses the browser already installed on the laptop (Microsoft Edge by default,
  or Chrome), so no extra browser download is needed.
* Runs with its **own profile** in ``data/browser_profile``: DUMBOO never sees
  the passwords or cookies of your normal browser. If you sign in to a site in
  DUMBOO's window yourself, that sign-in stays in DUMBOO's profile.
* Playwright's synchronous API must be used from one thread, so every browser
  operation is executed on a dedicated "Browser" thread; callers wait for the
  result with a timeout.
* Page content is untrusted data: it is returned as text and never treated as
  instructions. Password fields are never filled.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus, urlsplit

log = logging.getLogger(__name__)

_RESULTS_JS = """
() => {
  const out = [];
  for (const li of document.querySelectorAll('li.b_algo')) {
    const a = li.querySelector('h2 a');
    if (!a) continue;
    const cite = li.querySelector('cite');
    const p = li.querySelector('.b_caption p, p');
    out.push({title: a.innerText.trim(), url: a.href, display_url: cite ? cite.innerText.trim() : '',
              snippet: p ? p.innerText.trim() : ''});
  }
  if (out.length) return out;
  const host = location.hostname;
  for (const a of document.querySelectorAll('a[href^="http"]')) {
    const text = (a.innerText || '').trim();
    try { if (new URL(a.href).hostname.endsWith(host)) continue; } catch (e) { continue; }
    if (text.length > 15) out.push({title: text.slice(0, 200), url: a.href, display_url: a.href, snippet: ''});
    if (out.length >= 15) break;
  }
  return out;
}
"""

_FIELDS_JS = """
() => Array.from(document.querySelectorAll('input, textarea, select')).filter(e => {
  const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && e.type !== 'hidden';
}).slice(0, 60).map(e => {
  let label = '';
  if (e.id) { const l = document.querySelector(`label[for="${CSS.escape(e.id)}"]`); if (l) label = l.innerText; }
  if (!label && e.closest('label')) label = e.closest('label').innerText;
  return {tag: e.tagName.toLowerCase(), type: e.type || '', name: e.name || '', id: e.id || '',
          label: (label || e.getAttribute('aria-label') || e.placeholder || '').trim().slice(0, 80)};
})
"""


class BrowserError(RuntimeError):
    pass


class BrowserService:
    name = "browser"

    def __init__(self, settings: Callable[[], dict], profile_dir: Path) -> None:
        self._settings = settings
        self._profile_dir = profile_dir
        self._jobs: "queue.Queue[tuple[Callable[[], Any], Future] | None]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._pw = None
        self._context = None
        self._page = None
        self.last_error = ""

    # -- thread -----------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._context is not None

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="Browser", daemon=True)
                self._thread.start()

    def _loop(self) -> None:
        while True:
            item = self._jobs.get()
            if item is None:
                break
            fn, future = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn())
            except BaseException as exc:  # reported to the caller
                future.set_exception(exc)
        self._shutdown_browser()

    def call(self, fn: Callable[[], Any], timeout_s: float = 60.0) -> Any:
        self._ensure_thread()
        future: Future = Future()
        self._jobs.put((fn, future))
        try:
            return future.result(timeout=timeout_s)
        except FutureTimeout as exc:
            raise BrowserError("The browser did not respond in time.") from exc

    def stop(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._jobs.put(None)
            self._thread.join(timeout=8)
        self._thread = None

    def start(self) -> None:  # lifecycle hook: the browser itself starts on first use
        pass

    # -- on the browser thread -------------------------------------------------------------
    def _shutdown_browser(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._context = self._page = self._pw = None

    def _ctx(self):
        if self._context is not None:
            try:
                _ = self._context.pages
                return self._context
            except Exception:
                self._shutdown_browser()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserError("Browser automation needs the 'playwright' package (pip install playwright).") from exc
        settings = self._settings()
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        channel = settings.get("channel", "msedge")
        try:
            headless = bool(settings.get("headless", False))
            extra = {}
            if headless and settings.get("normal_user_agent"):
                # Headless Edge announces itself as "HeadlessChrome" and some sites (Bing) then return degraded
                # pages. Report the same Edge version without the "Headless" marker.
                probe = self._pw.chromium.launch(channel=None if channel == "chromium" else channel, headless=True)
                version = probe.version
                probe.close()
                major = version.split(".")[0]
                extra["user_agent"] = (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like "
                                       f"Gecko) Chrome/{major}.0.0.0 Safari/537.36 Edg/{version}")
            self._context = self._pw.chromium.launch_persistent_context(
                str(self._profile_dir), channel=None if channel == "chromium" else channel,
                headless=headless, accept_downloads=True, no_viewport=True, args=["--start-maximized"], **extra)
        except Exception as exc:
            self._pw.stop()
            self._pw = None
            raise BrowserError(f"DUMBOO's browser ({channel}) could not start: {str(exc).splitlines()[0][:200]}") from exc
        self._context.on("close", lambda *_: setattr(self, "_context", None))
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._context

    def _current(self):
        ctx = self._ctx()
        pages = [p for p in ctx.pages if not p.is_closed()]
        if self._page is None or self._page.is_closed():
            self._page = pages[-1] if pages else ctx.new_page()
        return self._page

    def _nav_timeout(self) -> int:
        return int(float(self._settings().get("navigation_timeout_s", 25)) * 1000)

    def _goto(self, page, url: str) -> dict:
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=self._nav_timeout())
        except Exception as exc:
            message = str(exc).splitlines()[0]
            match = re.search(r"net::(ERR_[A-Z_]+)", message)
            reason = {"ERR_NAME_NOT_RESOLVED": "the site name could not be found (no internet, or a typo)",
                      "ERR_INTERNET_DISCONNECTED": "there is no internet connection",
                      "ERR_CONNECTION_REFUSED": "the site refused the connection",
                      "ERR_CONNECTION_TIMED_OUT": "the connection timed out",
                      "ERR_CERT_AUTHORITY_INVALID": "the site's security certificate is not valid"}.get(
                match.group(1) if match else "", "the page did not load" if "Timeout" in message else message[:160])
            return {"ok": False, "url": url, "status": None, "title": "", "error": reason}
        status = response.status if response is not None else None
        try:
            if "bing.com/ck/" in page.url:  # search-result links go through a JavaScript redirect page
                page.wait_for_url(lambda u: "bing.com/ck/" not in u, timeout=10000)
            page.wait_for_load_state("load", timeout=6000)  # the title is usually set by then
        except Exception:
            pass
        try:
            title = page.title() or page.url
        except Exception:
            title = page.url
        ok = status is None or status < 400
        return {"ok": ok, "url": page.url, "status": status, "title": title,
                "error": "" if ok else f"the site answered with HTTP {status}"}

    # -- public operations (each runs on the browser thread) -------------------------------------
    def open(self, url: str, new_tab: bool = False) -> dict:
        def job():
            page = self._ctx().new_page() if new_tab else self._current()
            self._page = page
            result = self._goto(page, url)
            try:
                page.bring_to_front()
            except Exception:
                pass
            return result
        return self.call(job, self._nav_timeout() / 1000 + 20)

    def search(self, query: str) -> dict:
        engine = self._settings().get("search_engine", "https://www.bing.com/search?q={query}")
        url = engine.replace("{query}", quote_plus(query))

        def job():
            page = self._current()
            result = self._goto(page, url)
            if result["ok"]:
                try:
                    page.wait_for_selector("li.b_algo, #b_results, a", timeout=6000)
                except Exception:
                    pass
                result["results"] = [r for r in page.evaluate(_RESULTS_JS) if r.get("url", "").startswith("http")][:10]
            page.bring_to_front()
            return result
        return self.call(job, self._nav_timeout() / 1000 + 25)

    def tabs(self) -> list[dict]:
        def job():
            ctx = self._ctx()
            out = []
            for i, p in enumerate(p for p in ctx.pages if not p.is_closed()):
                try:
                    title = p.title()
                except Exception:
                    title = ""
                out.append({"index": i + 1, "title": title, "url": p.url, "active": p is self._page})
            return out
        return self.call(job, 30)

    def switch_tab(self, index: int) -> dict:
        def job():
            pages = [p for p in self._ctx().pages if not p.is_closed()]
            if not 1 <= index <= len(pages):
                raise BrowserError(f"There is no tab {index} (DUMBOO's browser has {len(pages)} tab(s)).")
            self._page = pages[index - 1]
            self._page.bring_to_front()
            return {"index": index, "title": self._page.title(), "url": self._page.url}
        return self.call(job, 30)

    def close_tab(self, index: int | None) -> dict:
        def job():
            pages = [p for p in self._ctx().pages if not p.is_closed()]
            target = self._current() if index is None else (pages[index - 1] if 1 <= index <= len(pages) else None)
            if target is None:
                raise BrowserError(f"There is no tab {index}.")
            title = target.title()
            target.close()
            remaining = [p for p in self._context.pages if not p.is_closed()] if self._context else []
            self._page = remaining[-1] if remaining else None
            return {"closed": title, "remaining": len(remaining)}
        return self.call(job, 30)

    def page_text(self, max_chars: int = 20000) -> dict:
        def job():
            page = self._current()
            try:
                text = page.inner_text("body", timeout=8000)
            except Exception:
                text = ""
            return {"url": page.url, "title": page.title(), "text": re.sub(r"\n{3,}", "\n\n", text)[:max_chars]}
        return self.call(job, 40)

    def results_on_page(self) -> list[dict]:
        return self.call(lambda: self._current().evaluate(_RESULTS_JS), 30)

    def download(self, url: str, dest_dir: Path, max_mb: int = 300) -> dict:
        """Download ``url`` with the browser's session into ``dest_dir`` (never overwrites, never opens)."""
        def job():
            ctx = self._ctx()
            response = ctx.request.get(url, timeout=self._nav_timeout() * 4, fail_on_status_code=False)
            if response.status >= 400:
                raise BrowserError(f"The download failed: the server answered HTTP {response.status}.")
            body = response.body()
            if not body:
                raise BrowserError("The server sent an empty file.")
            if len(body) > max_mb * 1024 * 1024:
                raise BrowserError(f"The file is larger than {max_mb} MB.")
            headers = response.headers
            name = ""
            disp = headers.get("content-disposition", "")
            m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disp, re.I)
            if m:
                name = m.group(1)
            if not name:
                name = Path(urlsplit(response.url).path).name or "download"
            ctype = headers.get("content-type", "")
            if "." not in name and "pdf" in ctype:
                name += ".pdf"
            return {"name": name, "body": body, "content_type": ctype, "final_url": response.url,
                    "expected": int(headers.get("content-length") or 0)}
        return self.call(job, self._nav_timeout() / 1000 * 4 + 30)

    def form_fields(self) -> list[dict]:
        return self.call(lambda: self._current().evaluate(_FIELDS_JS), 30)

    def fill(self, label: str, value: str) -> dict:
        def job():
            page = self._current()
            locators = [page.get_by_label(label, exact=False), page.get_by_placeholder(label, exact=False),
                        page.locator(f'[name="{label}"]'), page.locator(f"#{re.sub(r'[^A-Za-z0-9_-]', '', label)}")]
            for loc in locators:
                try:
                    if loc.count() == 0:
                        continue
                    target = loc.first
                    if (target.get_attribute("type") or "").lower() == "password":
                        raise BrowserError("DUMBOO never fills password fields. Please type passwords yourself.")
                    tag = target.evaluate("e => e.tagName.toLowerCase()")
                    if tag == "select":
                        target.select_option(label=value)
                        actual = target.evaluate("e => e.options[e.selectedIndex] ? e.options[e.selectedIndex].text : ''")
                    else:
                        target.fill(value, timeout=8000)
                        actual = target.input_value()
                    return {"label": label, "filled": actual == value or actual.strip() == value.strip()}
                except BrowserError:
                    raise
                except Exception:
                    continue
            raise BrowserError(f"I couldn't find a form field called '{label}' on this page.")
        return self.call(job, 40)

    def click_text(self, text: str) -> dict:
        def job():
            page = self._current()
            before = page.url
            for loc in (page.get_by_role("button", name=text), page.get_by_role("link", name=text),
                        page.get_by_text(text, exact=False)):
                try:
                    if loc.count() == 0:
                        continue
                    loc.first.click(timeout=8000)
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=8000)
                    except Exception:
                        pass
                    return {"clicked": text, "url_before": before, "url": page.url, "title": page.title()}
                except Exception:
                    continue
            raise BrowserError(f"I couldn't find '{text}' to click on this page.")
        return self.call(job, 40)

    def resolve(self, url: str, away_from: str, timeout_s: float = 15.0) -> str:
        """Follow a JavaScript redirect page (Google News / Bing links) to the real article URL."""
        def job():
            page = self._ctx().new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_s * 1000))
                try:
                    page.wait_for_url(lambda u: away_from not in u, timeout=int(timeout_s * 1000))
                except Exception:
                    pass
                return page.url
            finally:
                page.close()
        return self.call(job, timeout_s * 2 + 10)

    def read_rendered(self, url: str, timeout_s: float = 20.0) -> dict:
        """Text and HTML of a page after JavaScript ran (for pages that are empty without it)."""
        def job():
            page = self._ctx().new_page()
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_s * 1000))
                try:
                    page.wait_for_load_state("load", timeout=6000)
                except Exception:
                    pass
                return {"url": page.url, "status": response.status if response else None, "title": page.title(),
                        "html": page.content()[:3_000_000]}
            finally:
                page.close()
        return self.call(job, timeout_s * 2 + 10)

    def current(self) -> dict:
        def job():
            if self._context is None:
                return {"open": False}
            page = self._current()
            return {"open": True, "url": page.url, "title": page.title()}
        return self.call(job, 20)

    def close(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            try:
                self.call(self._shutdown_browser, 20)
            except Exception:
                pass

    @staticmethod
    def wait_s(seconds: float) -> None:
        time.sleep(seconds)
