"""Stand-ins for the Windows desktop, UI Automation, screen capture and DUMBOO's browser.

They behave like the real implementations closely enough to test planning,
policy, execution and verification without touching the real desktop.
"""

from __future__ import annotations

import itertools
import shutil
import threading
from pathlib import Path
from urllib.parse import urlsplit

from automation.platform.desktop import DesktopError, WindowInfo
from automation.platform.uia import Element


class FakeDesktop:
    name = "desktop"
    available = True

    def __init__(self, trash: Path | None = None) -> None:
        self._ids = itertools.count(1000)
        self.wins: dict[int, WindowInfo] = {}
        self.texts: dict[int, str] = {}
        self.selection: dict[int, str] = {}
        self.fg: int | None = None
        self.external: int | None = None
        self.clipboard: str | None = None
        self.seq = 1
        self.launched: list[tuple[str, list[str]]] = []
        self.opened: list[str] = []
        self.pressed: list[tuple[int | None, str]] = []
        self.clicks: list[tuple[int, int]] = []
        self.scrolled: list[tuple[int, int]] = []
        self.background: set[str] = set()      # processes running without a window
        self.trash = trash
        self.recycled: list[str] = []
        self.explorer: dict[int, str] = {}
        # behaviour switches
        self.fail_launch = False
        self.no_window = False
        self.block_focus = False
        self.keep_open_on_close = False
        self.block_new_document = False
        self.other_tabs: list[str] = []
        self.restore_text: dict[str, str] = {}      # process -> document a launched app reopens (Win11 Notepad)
        self.steal_focus_after: int | None = None   # chars typed before another window takes focus
        self.window_titles: dict[str, str] = {}     # process -> title for new windows

    # helpers for tests ---------------------------------------------------------------
    def add_window(self, process: str, title: str, text: str = "") -> WindowInfo:
        hwnd = next(self._ids)
        info = WindowInfo(hwnd, title, 4000 + hwnd, process, "FakeClass", False, False, (0, 0, 800, 600))
        self.wins[hwnd] = info
        self.texts[hwnd] = text
        self.fg = hwnd
        self.external = hwnd
        return info

    # Desktop interface ------------------------------------------------------------------
    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def windows(self) -> list[WindowInfo]:
        return list(self.wins.values())

    def window(self, hwnd: int) -> WindowInfo | None:
        return self.wins.get(hwnd)

    def foreground(self) -> WindowInfo | None:
        return self.wins.get(self.fg) if self.fg is not None else None

    def last_external(self) -> WindowInfo | None:
        return self.wins.get(self.external) if self.external is not None else None

    def focus(self, hwnd: int) -> bool:
        if self.block_focus or hwnd not in self.wins:
            return False
        self.wins[hwnd].minimized = False
        self.fg = hwnd
        self.external = hwnd
        return True

    def set_state(self, hwnd: int, state: str) -> bool:
        w = self.wins.get(hwnd)
        if w is None:
            return False
        w.minimized = state == "minimize"
        w.maximized = state == "maximize"
        return True

    def close(self, hwnd: int) -> bool:
        if hwnd not in self.wins:
            return False
        if not self.keep_open_on_close:
            self.wins.pop(hwnd)
            if self.fg == hwnd:
                self.fg = None
        return True

    def type_text(self, text: str, guard_hwnd: int, cancel: threading.Event) -> int:
        typed = 0
        for ch in text:
            if cancel.is_set():
                break
            if self.steal_focus_after is not None and typed >= self.steal_focus_after:
                self.fg = None
            if self.fg != guard_hwnd:
                raise DesktopError("Typing stopped: the target window is no longer in front.")
            self.texts[guard_hwnd] = self.texts.get(guard_hwnd, "") + ch
            typed += 1
        return typed

    def press(self, combo: str) -> None:
        self.pressed.append((self.fg, combo))
        if combo in ("ctrl+c", "ctrl+x") and self.fg in self.selection:
            self.clipboard = self.selection[self.fg]
            self.seq += 1
        elif combo == "ctrl+v" and self.fg is not None and self.clipboard:
            self.texts[self.fg] = self.texts.get(self.fg, "") + self.clipboard
        elif combo == "ctrl+n" and self.fg in self.wins and not self.block_new_document:
            self.other_tabs.append(self.texts.get(self.fg, ""))  # the old document stays in its own tab
            self.texts[self.fg] = ""   # a new blank document/tab in the same window
        elif combo == "ctrl+s" and self.fg in self.wins:
            self.wins[self.fg].title = self.wins[self.fg].title.lstrip("*").replace(" ●", "")

    def scroll(self, hwnd: int, clicks: int) -> None:
        self.scrolled.append((hwnd, clicks))

    def cursor_position(self) -> tuple[int, int]:
        return getattr(self, "_cursor", (100, 100))

    def move_cursor(self, x: int, y: int) -> None:
        self._cursor = (int(x), int(y))
        self.__dict__.setdefault("moves", []).append((int(x), int(y)))

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        self.clicks.append((x, y))

    def get_clipboard(self) -> str | None:
        return self.clipboard

    def set_clipboard(self, text: str) -> bool:
        self.clipboard = text
        self.seq += 1
        return True

    def clipboard_sequence(self) -> int:
        return self.seq

    def launch(self, exe: Path, args: list[str]) -> int:
        if self.fail_launch:
            raise OSError(2, "The system cannot find the file specified")
        self.launched.append((str(exe), list(args)))
        process = Path(exe).name
        if self.no_window:
            self.background.add(process.lower())
            return 1
        title = self.window_titles.get(process.lower(), Path(exe).stem)
        if args:
            title = f"{Path(args[0]).name} - {title}"
        info = self.add_window(process, title, self.restore_text.get(process.lower(), ""))
        if process.lower() == "explorer.exe" and args:
            self.explorer[info.hwnd] = args[0]
        return 4000 + info.hwnd

    def shell_open(self, target: str) -> None:
        self.opened.append(target)
        if target.startswith("http"):
            host = (urlsplit(target).hostname or "").replace("www.", "")
            site = host.split(".")[0].capitalize()
            browsers = [w for w in self.wins.values() if w.process.lower() == "chrome.exe"]
            if browsers:
                browsers[0].title = f"{site} - Google Chrome"
                self.fg = browsers[0].hwnd
            else:
                self.add_window("chrome.exe", f"{site} - Google Chrome")
        elif Path(target).is_dir():
            info = self.add_window("explorer.exe", Path(target).name)
            self.explorer[info.hwnd] = target
        else:
            self.add_window("notepad.exe", f"{Path(target).name} - Notepad")

    def process_running(self, name: str) -> list[int]:
        name = name.lower()
        pids = [w.pid for w in self.wins.values() if w.process.lower() == name]
        return pids or ([1] if name in self.background else [])

    def explorer_folders(self) -> list[tuple[int, str]]:
        return [(h, p) for h, p in self.explorer.items() if h in self.wins]

    def recycle(self, paths: list[Path]) -> bool:
        for p in paths:
            self.recycled.append(str(p))
            if self.trash is not None:
                self.trash.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(self.trash / Path(p).name))
        return True


class FakeUIA:
    """Implements the functions of automation.platform.uia used by the capabilities."""

    def __init__(self, desktop: FakeDesktop) -> None:
        self.desktop = desktop
        self.items: dict[int, list[dict]] = {}
        self.elements: dict[int, list[Element]] = {}
        self.invoked: list[str] = []
        self.readable = True

    def read_window(self, hwnd: int, **_kw) -> dict:
        info = self.desktop.window(hwnd)
        items = self.items.get(hwnd, [])
        return {"title": info.title if info else "", "items": items,
                "text": "\n".join(i.get("value") or i["name"] for i in items)}

    def find_elements(self, hwnd: int, query: str, control_type=None, limit: int = 10, **_kw) -> list[Element]:
        q = query.lower()
        found = [e for e in self.elements.get(hwnd, []) if q in e.name.lower()
                 and (control_type is None or e.control_type == control_type)]
        return found[:limit]

    def invoke(self, hwnd: int, element: Element, **_kw) -> str:
        self.invoked.append(element.name)
        return "invoke"

    def focused_text(self, hwnd: int, limit: int = 200000) -> str | None:
        return self.desktop.texts.get(hwnd) if self.readable else None


def fake_capture(rect=None):
    return b"\x89PNG\r\n\x1a\nFAKE", (800, 600)


class FakeBrowser:
    name = "browser"

    def __init__(self) -> None:
        self.pages: list[dict] = []
        self.current_index = -1
        self.failing: dict[str, str] = {}
        self.results = [{"title": "Welcome to Python.org", "url": "https://www.python.org/", "display_url":
                         "python.org", "snippet": "The official home of Python"},
                        {"title": "3.13 Documentation", "url": "https://docs.python.org/3/", "display_url":
                         "docs.python.org", "snippet": "Python documentation"}]
        self.files: dict[str, bytes] = {}
        self.filled: dict[str, str] = {}
        self.clicked: list[str] = []
        self.last_results: list[dict] = []
        self.redirects: dict[str, str] = {}   # research: Google News link -> article URL
        self.rendered: dict[str, str] = {}    # research: JavaScript-rendered HTML per URL
        self.resolved: list[str] = []

    def _page(self, url: str) -> dict:
        if url in self.failing:
            return {"ok": False, "url": url, "status": None, "title": "", "error": self.failing[url]}
        title = (urlsplit(url).hostname or url).replace("www.", "")
        return {"ok": True, "url": url, "status": 200, "title": f"{title} page"}

    def open(self, url: str, new_tab: bool = False) -> dict:
        result = self._page(url)
        if result["ok"]:
            page = {"url": url, "title": result["title"], "text": f"Content of {url}"}
            if new_tab or self.current_index < 0:
                self.pages.append(page)
                self.current_index = len(self.pages) - 1
            else:
                self.pages[self.current_index] = page
        return result

    def search(self, query: str) -> dict:
        result = self.open("https://www.bing.com/search?q=" + query.replace(" ", "+"))
        self.last_results = list(self.results)
        result["results"] = self.last_results
        return result

    def results_on_page(self) -> list[dict]:
        return list(self.last_results)

    def tabs(self) -> list[dict]:
        return [{"index": i + 1, "title": p["title"], "url": p["url"], "active": i == self.current_index}
                for i, p in enumerate(self.pages)]

    def switch_tab(self, index: int) -> dict:
        self.current_index = index - 1
        p = self.pages[self.current_index]
        return {"index": index, "title": p["title"], "url": p["url"]}

    def close_tab(self, index: int | None) -> dict:
        i = self.current_index if index is None else index - 1
        page = self.pages.pop(i)
        self.current_index = len(self.pages) - 1
        return {"closed": page["title"], "remaining": len(self.pages)}

    def page_text(self, max_chars: int = 20000) -> dict:
        if self.current_index < 0:
            return {"url": "", "title": "", "text": ""}
        p = self.pages[self.current_index]
        return {"url": p["url"], "title": p["title"], "text": p["text"]}

    def current(self) -> dict:
        if self.current_index < 0:
            return {"open": False}
        p = self.pages[self.current_index]
        return {"open": True, "url": p["url"], "title": p["title"]}

    def download(self, url: str, dest_dir: Path, max_mb: int = 300) -> dict:
        if url not in self.files:
            from automation.platform.browser import BrowserError
            raise BrowserError("The download failed: the server answered HTTP 404.")
        body = self.files[url]
        return {"name": Path(urlsplit(url).path).name, "body": body, "content_type": "application/pdf",
                "final_url": url, "expected": len(body)}

    def form_fields(self) -> list[dict]:
        return [{"tag": "input", "type": "text", "name": "email", "id": "email", "label": "Email"}]

    def fill(self, label: str, value: str) -> dict:
        self.filled[label] = value
        return {"label": label, "filled": True}

    def click_text(self, text: str) -> dict:
        self.clicked.append(text)
        url = self.pages[self.current_index]["url"] if self.pages else ""
        return {"clicked": text, "url_before": url, "url": url + "#clicked", "title": "after click"}

    def resolve(self, url: str, away_from: str, timeout_s: float = 15.0) -> str:
        self.resolved.append(url)
        return self.redirects.get(url, url)

    def read_rendered(self, url: str, timeout_s: float = 20.0) -> dict:
        html = self.rendered.get(url, "")
        return {"url": url, "status": 200 if html else 404, "title": "", "html": html}

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass
