"""Shared result types for the Guardian (all plain data, JSON-serialisable via ``asdict``)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

PASS, WARNING, FAIL, UNKNOWN, NOT_SUPPORTED = "PASS", "WARNING", "FAIL", "UNKNOWN", "NOT_SUPPORTED"
STATUSES = (PASS, WARNING, FAIL, UNKNOWN, NOT_SUPPORTED)

# Assessment of an investigated item (never "malicious" without threat-intelligence evidence).
NO_CONCERN = "No obvious concern"
SUSPICIOUS = "Suspicious indicators"
HIGH_CONCERN = "High concern"
INCONCLUSIVE = "Inconclusive"


class GuardianError(Exception):
    """A check could not run. ``kind``: permission | timeout | unavailable | cancelled | invalid."""

    def __init__(self, message: str, kind: str = "unavailable") -> None:
        super().__init__(message)
        self.kind = kind
        self.user_message = message


@dataclass
class CheckResult:
    id: str
    title: str
    status: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)
    source: str = ""                 # where the information came from (API, command, file)
    advice: str = ""                 # what to do about a WARNING / FAIL

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Signature:
    status: str                      # Valid | NotSigned | HashMismatch | NotTrusted | UnknownError | NOT_SUPPORTED | UNKNOWN
    signer: str = ""                 # certificate subject CN (only when the signature was checked)
    issuer: str = ""

    @property
    def valid(self) -> bool:
        return self.status == "Valid"

    @property
    def publisher(self) -> str:
        if not self.valid or not self.signer:
            return "UNKNOWN"
        return self.signer


@dataclass
class FileFacts:
    path: str
    exists: bool
    size: int | None = None
    modified: str | None = None      # ISO time
    created: str | None = None
    sha256: str | None = None
    error: str = ""                  # why something could not be read


@dataclass
class StartupEntry:
    id: str                          # stable id (mechanism + location + name)
    name: str
    mechanism: str                   # registry_run | startup_folder | scheduled_task | service
    location: str                    # registry key / folder / task path / service name
    command: str                     # command line, secrets redacted
    executable: str | None           # resolved file the entry starts (None if it could not be resolved)
    scope: str = "user"              # user | machine
    enabled: bool = True
    signature: Signature | None = None
    flags: list[str] = field(default_factory=list)   # quick observations from the listing


@dataclass
class Indicator:
    text: str
    weight: int                      # +3 strong concern … -3 strong reassurance
    kind: str = "observed"           # observed | external | inferred


@dataclass
class Investigation:
    subject: str
    kind: str                        # startup | process | file
    facts: dict[str, Any]
    indicators: list[Indicator]
    intel: dict[str, Any]            # {"provider", "status", ...} or {"status": "NOT_CHECKED", "reason"}
    assessment: str
    confidence: str                  # High | Medium | Low
    limitations: list[str]
    recommendation: str

    def to_dict(self) -> dict:
        return asdict(self)
