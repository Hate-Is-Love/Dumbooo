"""Software updates and security advisories.

* **Available updates** come from ``winget upgrade`` (Windows Package Manager, Microsoft's official
  tool): installed version vs. the newest version in winget's sources. An available update is
  *not* proof of a vulnerability.
* **Advisories** come from CISA's Known Exploited Vulnerabilities (KEV) catalogue, an official U.S.
  government list of vulnerabilities attacked in the wild (public download; nothing about this
  PC is sent). A product match means "exploited flaws exist for this product": the installed version
  is *not* verified against the affected range unless a version is stated, so it says so.
Nothing is installed or downloaded-and-run here; updating is always the user's action.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from datetime import datetime

from guardian.models import GuardianError

KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"


def parse_winget_table(text: str) -> list[dict]:
    """Parse winget's fixed-width table (Name, Id, Version, Available, Source)."""
    lines = [l.rstrip("\r") for l in text.splitlines()]
    header_i = next((i for i, l in enumerate(lines) if re.match(r"^Name\s+Id\s+Version\s+Available", l)), None)
    if header_i is None:
        return []
    header = lines[header_i]
    starts = [header.index(c) for c in ("Name", "Id", "Version", "Available")]
    source_at = header.find("Source")
    rows = []
    for line in lines[header_i + 2:]:
        if not line.strip() or re.match(r"^\d+ upgrades? available", line.strip()) or len(line) < starts[3]:
            continue

        def cut(a, b):
            return line[a:b].strip() if b is not None else line[a:].strip()
        rows.append({"name": cut(starts[0], starts[1]), "id": cut(starts[1], starts[2]),
                     "installed": cut(starts[2], starts[3]),
                     "available": cut(starts[3], source_at if source_at > 0 else None),
                     "source": cut(source_at, None) if source_at > 0 else ""})
    return [r for r in rows if r["name"] and r["available"]]


def winget_upgrades(run=None, timeout: float = 90.0) -> list[dict]:
    if sys.platform != "win32" and run is None:
        raise GuardianError("winget is only available on Windows", "unavailable")
    if run is None:
        def run():
            try:
                done = subprocess.run(["winget", "upgrade", "--disable-interactivity"], capture_output=True,
                                      timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except FileNotFoundError as exc:
                raise GuardianError("winget (Windows Package Manager) is not installed", "unavailable") from exc
            except subprocess.TimeoutExpired as exc:
                raise GuardianError(f"winget did not answer within {timeout:.0f} s", "timeout") from exc
            return done.stdout.decode("utf-8", "replace")
    text = run()
    if "agreement" in text.lower() and "Name" not in text:
        raise GuardianError("winget needs you to accept its source agreements once (run 'winget upgrade' in a "
                            "terminal). DUMBOO doesn't accept terms for you.", "unavailable")
    return parse_winget_table(text)


_STOP = {"microsoft", "windows", "the", "for", "and", "x64", "x86", "redistributable", "visual", "c++", "driver",
         "server", "sql", "update", "system", "software", "app", "client", "desktop", "player", "media"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9][a-z0-9.+-]{2,}", text.lower()) if t not in _STOP}


def kev_matches(installed: list[dict], catalogue: dict, limit_per_product: int = 3) -> list[dict]:
    """KEV entries whose product name clearly matches an installed app (name tokens overlap fully)."""
    vulns = catalogue.get("vulnerabilities") or []
    out = []
    for app in installed:
        app_tokens = _tokens(app["name"])
        if not app_tokens:
            continue
        hits = []
        for v in vulns:
            product_tokens = _tokens(v.get("product", ""))
            if product_tokens and product_tokens <= app_tokens and (_tokens(v.get("vendorProject", "")) & app_tokens
                                                                     or product_tokens == app_tokens):
                hits.append(v)
        hits.sort(key=lambda v: v.get("dateAdded", ""), reverse=True)
        for v in hits[:limit_per_product]:
            out.append({"app": app["name"], "installed": app.get("installed", ""), "cve": v.get("cveID"),
                        "vendor": v.get("vendorProject"), "product": v.get("product"),
                        "title": v.get("vulnerabilityName"), "added": v.get("dateAdded"),
                        "action": v.get("requiredAction"),
                        "link": f"https://nvd.nist.gov/vuln/detail/{v.get('cveID')}",
                        "version_verified": False})
    return out


def fetch_kev(client_factory=None, timeout: float = 20.0) -> dict:
    if client_factory is None:
        import httpx

        def client_factory():
            return httpx.Client(timeout=timeout, headers={"User-Agent": "DUMBOO-Guardian"}, follow_redirects=True)
    try:
        with client_factory() as client:
            r = client.get(KEV_URL)
    except Exception as exc:
        raise GuardianError(f"the CISA advisory catalogue could not be downloaded ({exc.__class__.__name__})",
                            "unavailable") from exc
    if r.status_code != 200:
        raise GuardianError(f"the CISA advisory catalogue answered with HTTP {r.status_code}", "unavailable")
    try:
        data = r.json()
    except ValueError as exc:
        raise GuardianError("the CISA advisory catalogue could not be read", "unavailable") from exc
    data["_fetched"] = datetime.now().isoformat(timespec="seconds")
    return data


class AdvisoryCache:
    """Keeps the KEV catalogue for 12 hours."""

    def __init__(self, fetch=fetch_kev, clock=time.monotonic) -> None:
        self._fetch, self._clock = fetch, clock
        self._data: dict | None = None
        self._at = -1e9

    def get(self) -> dict:
        if self._data is None or self._clock() - self._at > 12 * 3600:
            self._data, self._at = self._fetch(), self._clock()
        return self._data
