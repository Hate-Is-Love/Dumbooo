"""Optional threat-intelligence lookups (hash only). Off unless the user enables external lookups
and adds a key for the chosen provider. Nothing is uploaded: only the SHA-256 is sent, which
still tells the provider that this file exists somewhere, so every lookup is disclosed in the report.

* VirusTotal (``VIRUSTOTAL_API_KEY``): how many antivirus engines flag the hash.
* MalwareBazaar by abuse.ch (``MALWAREBAZAAR_API_KEY``): whether the hash is a known malware sample.

Results: ``{"provider", "status": MALICIOUS|SUSPICIOUS|CLEAN|NOT_FOUND|NOT_CHECKED|ERROR, ...}``.
"Not found" means only that the provider has never seen the file, not that it is safe.
"""

from __future__ import annotations

import logging
from typing import Any

from guardian.config import GuardianConfig

log = logging.getLogger(__name__)


def not_checked(reason: str) -> dict:
    return {"status": "NOT_CHECKED", "reason": reason}


class VirusTotal:
    name = "virustotal"
    URL = "https://www.virustotal.com/api/v3/files/{}"

    def __init__(self, key: str, client) -> None:
        self._key, self._client = key, client

    def lookup(self, sha256: str) -> dict:
        try:
            r = self._client.get(self.URL.format(sha256), headers={"x-apikey": self._key}, timeout=12.0)
        except Exception as exc:
            return {"provider": "VirusTotal", "status": "ERROR", "reason": f"request failed ({exc.__class__.__name__})"}
        if r.status_code == 404:
            return {"provider": "VirusTotal", "status": "NOT_FOUND",
                    "reason": "VirusTotal has no record of this file (this does not mean it is safe)"}
        if r.status_code == 429:
            return {"provider": "VirusTotal", "status": "ERROR", "reason": "rate limit reached (free API: 4 lookups/min)"}
        if r.status_code in (401, 403):
            return {"provider": "VirusTotal", "status": "ERROR", "reason": "the API key was rejected"}
        if r.status_code != 200:
            return {"provider": "VirusTotal", "status": "ERROR", "reason": f"HTTP {r.status_code}"}
        try:
            attrs = r.json()["data"]["attributes"]
            stats = attrs["last_analysis_stats"]
        except (ValueError, KeyError):
            return {"provider": "VirusTotal", "status": "ERROR", "reason": "unreadable answer"}
        malicious, suspicious = int(stats.get("malicious", 0)), int(stats.get("suspicious", 0))
        engines = sum(int(v) for v in stats.values())
        status = "MALICIOUS" if malicious >= 3 else ("SUSPICIOUS" if malicious + suspicious > 0 else "CLEAN")
        return {"provider": "VirusTotal", "status": status, "malicious": malicious, "suspicious": suspicious,
                "engines": engines, "name": attrs.get("meaningful_name", ""),
                "link": f"https://www.virustotal.com/gui/file/{sha256}"}


class MalwareBazaar:
    name = "malwarebazaar"
    URL = "https://mb-api.abuse.ch/api/v1/"

    def __init__(self, key: str, client) -> None:
        self._key, self._client = key, client

    def lookup(self, sha256: str) -> dict:
        try:
            r = self._client.post(self.URL, data={"query": "get_info", "hash": sha256},
                                  headers={"Auth-Key": self._key}, timeout=12.0)
        except Exception as exc:
            return {"provider": "MalwareBazaar", "status": "ERROR", "reason": f"request failed ({exc.__class__.__name__})"}
        if r.status_code in (401, 403):
            return {"provider": "MalwareBazaar", "status": "ERROR", "reason": "the API key was rejected"}
        if r.status_code != 200:
            return {"provider": "MalwareBazaar", "status": "ERROR", "reason": f"HTTP {r.status_code}"}
        try:
            data = r.json()
        except ValueError:
            return {"provider": "MalwareBazaar", "status": "ERROR", "reason": "unreadable answer"}
        state = data.get("query_status")
        if state == "hash_not_found":
            return {"provider": "MalwareBazaar", "status": "NOT_FOUND",
                    "reason": "not a known malware sample in MalwareBazaar (this does not mean it is safe)"}
        if state == "ok" and data.get("data"):
            sample = data["data"][0]
            return {"provider": "MalwareBazaar", "status": "MALICIOUS", "family": sample.get("signature") or "",
                    "first_seen": sample.get("first_seen", ""),
                    "link": f"https://bazaar.abuse.ch/sample/{sha256}/"}
        return {"provider": "MalwareBazaar", "status": "ERROR", "reason": f"unexpected answer ({state})"}


def provider_for(cfg: GuardianConfig, client_factory=None) -> Any | None:
    """The configured provider, or None (with the reason available through ``why_not``)."""
    if not cfg.intel_ready:
        return None
    if client_factory is None:
        import httpx

        def client_factory():
            return httpx.Client(timeout=12.0, headers={"User-Agent": "DUMBOO-Guardian"})
    client = client_factory()
    cls = {"virustotal": VirusTotal, "malwarebazaar": MalwareBazaar}[cfg.threat_intel_provider]
    return cls(cfg.key_for(cfg.threat_intel_provider), client)


def why_not(cfg: GuardianConfig) -> str:
    if not cfg.external_lookups:
        return "external lookups are off (SECURITY → SETTINGS)"
    if cfg.threat_intel_provider == "none":
        return "no threat-intelligence provider is chosen"
    return f"no API key for {cfg.threat_intel_provider} in .env"
