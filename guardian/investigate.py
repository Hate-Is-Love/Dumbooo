"""Investigating one startup entry, process or file: collect evidence, assess, recommend."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

from guardian.assess import assess, intel_indicators, recommendation, signature_indicators
from guardian.files import command_observations, file_facts, path_observations
from guardian.intel import not_checked
from guardian.models import INCONCLUSIVE, Indicator, Investigation, Signature, StartupEntry

MECHANISMS = {"registry_run": "Registry Run key", "startup_folder": "Startup folder",
              "scheduled_task": "Scheduled task", "service": "Automatic service"}


def investigate_file(subject: str, kind: str, path: str | None, command: str, signature: Signature | None,
                     intel_lookup, extra_facts: dict | None = None, extra_indicators: list[Indicator] | None = None,
                     max_mb: int = 512, cancel: threading.Event | None = None, can_disable: bool = False,
                     now: datetime | None = None, why_no_intel: str = "no threat-intelligence lookup was available"
                     ) -> Investigation:
    """Shared by startup entries, processes and files. ``intel_lookup(sha256) -> dict`` or None."""
    now = now or datetime.now()
    facts_obj = file_facts(path, True, max_mb, cancel)
    facts = {**(extra_facts or {}), "Executable": path or "could not be determined",
             "File exists": "yes" if facts_obj.exists else "no", "Command": command or "—"}
    if facts_obj.size is not None:
        facts["Size"] = f"{facts_obj.size:,} bytes"
    if facts_obj.modified:
        facts["Modified"] = facts_obj.modified.replace("T", " ")
    if facts_obj.created:
        facts["Created"] = facts_obj.created.replace("T", " ")
    facts["SHA-256"] = facts_obj.sha256 or "not calculated"
    facts["Publisher"] = signature.publisher if signature else "UNKNOWN"
    facts["Signature"] = signature.status if signature else "not checked"
    limitations: list[str] = []
    if facts_obj.error:
        limitations.append(facts_obj.error.capitalize())
    indicators: list[Indicator] = list(extra_indicators or [])
    known = {i.text for i in indicators}
    indicators += [Indicator(t, w) for t, w in path_observations(path) if t not in known]
    indicators += [Indicator(t, w) for t, w in command_observations(command)]
    if not facts_obj.exists:
        indicators.append(Indicator("The file it points to does not exist (often left over from an uninstalled "
                                    "program)", 0))
    indicators += signature_indicators(signature)
    if signature is None or signature.status in ("UNKNOWN", "NOT_SUPPORTED"):
        limitations.append("The digital signature could not be checked")
    if facts_obj.modified:
        try:
            if now - datetime.fromisoformat(facts_obj.modified) < timedelta(days=7):
                indicators.append(Indicator("Changed in the last 7 days (context only: updates do this too)", 0,
                                            "inferred"))
        except ValueError:
            pass
    if facts_obj.sha256 and intel_lookup is not None:
        intel = intel_lookup(facts_obj.sha256)
    elif not facts_obj.sha256:
        intel = not_checked("no hash (file missing or unreadable)")
    else:
        intel = not_checked(why_no_intel)
    indicators += intel_indicators(intel)
    if intel.get("status") in ("NOT_CHECKED", "ERROR"):
        limitations.append(f"Threat intelligence not checked: {intel.get('reason')}")
    elif intel.get("status") == "NOT_FOUND":
        limitations.append(intel.get("reason", "Unknown to the threat-intelligence provider"))
    signature_checked = signature is not None and signature.status not in ("UNKNOWN", "NOT_SUPPORTED", "UnknownError")
    assessment, confidence = assess(indicators, facts_obj.exists, signature_checked, bool(facts_obj.sha256),
                                    intel.get("status") in ("CLEAN", "MALICIOUS", "SUSPICIOUS", "NOT_FOUND"))
    if not facts_obj.exists:
        assessment = INCONCLUSIVE
    limitations.append("Local checks cannot prove a file is safe; they show what is unusual.")
    return Investigation(subject, kind, facts, indicators, intel, assessment, confidence, limitations,
                         recommendation(assessment, kind, can_disable))


def investigate_entry(entry: StartupEntry, signature: Signature | None, intel_lookup, why_no_intel: str,
                      max_mb: int = 512, cancel: threading.Event | None = None) -> Investigation:
    extra = {"Startup entry": entry.name, "Mechanism": MECHANISMS.get(entry.mechanism, entry.mechanism),
             "Location": entry.location, "Applies to": "all users" if entry.scope == "machine" else "this user",
             "Enabled": "yes" if entry.enabled else "no"}
    can_disable = entry.mechanism in ("registry_run", "startup_folder") and entry.scope == "user"
    return investigate_file(entry.name, "startup", entry.executable, entry.command, signature, intel_lookup, extra,
                            max_mb=max_mb, cancel=cancel, can_disable=can_disable, why_no_intel=why_no_intel)
