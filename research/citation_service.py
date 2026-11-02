"""Citation validation: the AI may only cite sources DUMBOO actually retrieved.

* Source numbers that don't exist are removed (and counted).
* Any URL the AI writes that isn't one of the retrieved sources is removed.
* A factual statement left without any valid source is not presented as a fact.
* Dates are never taken from the AI: a development's publication date comes from the
  cited sources' metadata, and an event date is kept only if it appears in their text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from research.models import Finding, ResearchResult
from research.quality import canonical_url

_URL = re.compile(r"https?://[^\s)\]>\"']+")
_REF = re.compile(r"\[(\d{1,2}(?:\s*,\s*\d{1,2})*)\]")


@dataclass
class CitationReport:
    removed_ids: int = 0
    removed_urls: int = 0
    unsupported: int = 0

    def notes(self) -> list[str]:
        out = []
        if self.removed_ids or self.removed_urls:
            out.append(f"{self.removed_ids + self.removed_urls} citation(s) that didn't match a retrieved source "
                       "were removed.")
        if self.unsupported:
            out.append(f"{self.unsupported} statement(s) without a supporting source were left out.")
        return out


class CitationService:
    def __init__(self, result: ResearchResult) -> None:
        self._result = result
        self._ids = {s.id for s in result.sources}
        self._urls = {canonical_url(s.url) for s in result.sources} | {
            canonical_url(s.original_url) for s in result.sources if s.original_url}
        self.report = CitationReport()

    def clean_ids(self, ids: list[int]) -> list[int]:
        valid = [i for i in ids if i in self._ids]
        self.report.removed_ids += len(ids) - len(valid)
        return valid

    def clean_text(self, text: str) -> str:
        """Remove unknown [n] markers and URLs that aren't retrieved sources."""
        def ref(m: re.Match) -> str:
            nums = [int(n) for n in re.split(r"\s*,\s*", m.group(1))]
            ok = [n for n in nums if n in self._ids]
            self.report.removed_ids += len(nums) - len(ok)
            return "".join(f"[{n}]" for n in ok)

        def url(m: re.Match) -> str:
            if canonical_url(m.group(0)) in self._urls:
                return m.group(0)
            self.report.removed_urls += 1
            return "(link removed: not a retrieved source)"

        return _URL.sub(url, _REF.sub(ref, text or "")).strip()

    def ids_in(self, text: str) -> list[int]:
        found: list[int] = []
        for m in _REF.finditer(text or ""):
            for n in re.split(r"\s*,\s*", m.group(1)):
                if int(n) in self._ids and int(n) not in found:
                    found.append(int(n))
        return found

    def finalize(self, findings: list[Finding], require_sources: bool = True) -> list[Finding]:
        out = []
        for f in findings:
            f.text = self.clean_text(f.text)
            f.why = self.clean_text(f.why)
            f.uncertainty = self.clean_text(f.uncertainty)
            f.source_ids = self.clean_ids(f.source_ids) or self.ids_in(f.text)
            f.contradicting_ids = self.clean_ids(f.contradicting_ids)
            if require_sources and not f.source_ids and not f.contradicting_ids and f.kind != "analysis":
                if f.verdict in ("insufficient",):
                    out.append(f)
                    continue
                self.report.unsupported += 1
                continue
            out.append(f)
        return out

    def event_date_supported(self, event_date: str, source_ids: list[int]) -> bool:
        """Keep an AI-reported event date only if the cited sources' text actually contains it."""
        tokens = [t for t in re.findall(r"[A-Za-z]{3,}|\d{1,4}", event_date or "") if t.lower() not in ("the", "on")]
        if not tokens:
            return False
        for sid in source_ids:
            s = self._result.source(sid)
            hay = " ".join(s.passages + [s.snippet, s.title]).lower() if s else ""
            if hay and all(t.lower()[:3] in hay for t in tokens):
                return True
        return False
