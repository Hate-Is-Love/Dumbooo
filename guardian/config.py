"""Guardian configuration: the ``security`` settings section plus optional keys from ``.env``.

Environment overrides (optional, for that run only): ``SECURITY_MONITOR_ENABLED``,
``SECURITY_EXTERNAL_LOOKUPS_ENABLED``, ``SECURITY_THREAT_INTEL_PROVIDER``,
``SECURITY_SCAN_TIMEOUT_SECONDS``, ``SECURITY_FILE_INTEGRITY_ENABLED``.
Keys (only from the environment / ``.env``, never stored or logged): ``VIRUSTOTAL_API_KEY``,
``MALWAREBAZAAR_API_KEY``. Automatic remediation does not exist: every change needs the user's
confirmation (``SECURITY_AUTO_REMEDIATION`` is accepted only as ``false``).
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Mapping

PROVIDERS = ("none", "virustotal", "malwarebazaar")
KEY_VARIABLES = {"virustotal": "VIRUSTOTAL_API_KEY", "malwarebazaar": "MALWAREBAZAAR_API_KEY"}


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


ENV = {
    "SECURITY_MONITOR_ENABLED": ("monitor_enabled", _bool),
    "SECURITY_EXTERNAL_LOOKUPS_ENABLED": ("external_lookups", _bool),
    "SECURITY_THREAT_INTEL_PROVIDER": ("threat_intel_provider", lambda v: v.strip().lower()),
    "SECURITY_SCAN_TIMEOUT_SECONDS": ("scan_timeout_s", float),
    "SECURITY_FILE_INTEGRITY_ENABLED": ("file_integrity_enabled", _bool),
}


@dataclass(frozen=True)
class GuardianConfig:
    monitor_enabled: bool = True
    external_lookups: bool = False         # hash / URL reputation lookups send data out: off by default
    threat_intel_provider: str = "none"
    advisory_feeds: bool = True            # download public advisory catalogues (nothing about you is sent)
    scan_timeout_s: float = 120.0
    file_integrity_enabled: bool = True
    max_hash_mb: int = 512
    max_integrity_files: int = 20000
    virustotal_key: str = ""
    malwarebazaar_key: str = ""

    @classmethod
    def from_settings(cls, section: Mapping[str, Any], environ: Mapping[str, str]) -> "GuardianConfig":
        names = {f.name for f in fields(cls)}
        values = {k: v for k, v in section.items() if k in names and not k.endswith("_key")}
        for var, (key, parse) in ENV.items():
            raw = environ.get(var)
            if raw:
                try:
                    values[key] = parse(raw)
                except ValueError:
                    pass
        if values.get("threat_intel_provider") not in PROVIDERS:
            values["threat_intel_provider"] = "none"
        return cls(**values, virustotal_key=(environ.get("VIRUSTOTAL_API_KEY") or "").strip(),
                   malwarebazaar_key=(environ.get("MALWAREBAZAAR_API_KEY") or "").strip())

    def key_for(self, provider: str) -> str:
        return {"virustotal": self.virustotal_key, "malwarebazaar": self.malwarebazaar_key}.get(provider, "")

    @property
    def intel_ready(self) -> bool:
        return self.external_lookups and self.threat_intel_provider != "none" and bool(
            self.key_for(self.threat_intel_provider))
