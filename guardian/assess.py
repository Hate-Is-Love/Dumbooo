"""Turning collected evidence into an assessment, confidence and next step (deterministic rules).

Rules: only threat intelligence can make something "malicious"; local observations can make it
*suspicious*. Missing evidence lowers confidence instead of being guessed.
"""

from __future__ import annotations

from guardian.models import HIGH_CONCERN, INCONCLUSIVE, NO_CONCERN, SUSPICIOUS, Indicator, Signature

KNOWN_GOOD_ISSUERS = ("microsoft", "digicert", "sectigo", "globalsign", "entrust", "ssl.com", "comodo", "certum",
                      "go daddy", "godaddy", "symantec", "thawte", "verisign", "apple", "google")


def signature_indicators(sig: Signature | None) -> list[Indicator]:
    if sig is None:
        return []
    if sig.status == "Valid":
        return [Indicator(f"Digitally signed by {sig.signer} (signature valid)", -2)]
    if sig.status == "NotSigned":
        return [Indicator("Not digitally signed (many legitimate small tools are unsigned too)", 1)]
    if sig.status in ("HashMismatch",):
        return [Indicator("Signature is broken: the file was changed after it was signed", 3)]
    if sig.status in ("NotTrusted", "UnknownError"):
        return [Indicator(f"Signature could not be verified ({sig.status})", 2 if sig.status == "NotTrusted" else 0)]
    return []


def intel_indicators(intel: dict) -> list[Indicator]:
    s = intel.get("status")
    if s == "MALICIOUS":
        detail = (f"{intel.get('malicious')} of {intel.get('engines')} antivirus engines flag it" if "engines" in intel
                  else f"known malware sample{(' (' + intel['family'] + ')') if intel.get('family') else ''}")
        return [Indicator(f"{intel.get('provider')}: {detail}", 5, "external")]
    if s == "SUSPICIOUS":
        return [Indicator(f"{intel.get('provider')}: flagged by {intel.get('malicious', 0) + intel.get('suspicious', 0)} "
                          f"engine(s)", 2, "external")]
    if s == "CLEAN":
        return [Indicator(f"{intel.get('provider')}: {intel.get('engines')} engines found nothing", -2, "external")]
    return []


def assess(indicators: list[Indicator], have_file: bool, signature_checked: bool, hashed: bool,
           intel_checked: bool) -> tuple[str, str]:
    """(assessment, confidence)."""
    concern = sum(i.weight for i in indicators if i.weight > 0)
    reassurance = -sum(i.weight for i in indicators if i.weight < 0)
    net = concern - reassurance
    strong = any(i.weight >= 3 for i in indicators)   # e.g. encoded PowerShell, system-name disguise
    if any(i.kind == "external" and i.weight >= 5 for i in indicators):
        assessment = HIGH_CONCERN                     # only threat intelligence can say "known malicious"
    elif not have_file:
        assessment = INCONCLUSIVE
    elif net >= 6 or (strong and net >= 4):
        assessment = HIGH_CONCERN
    elif net >= 2 or strong:
        assessment = SUSPICIOUS
    elif not signature_checked and concern == 0 and reassurance == 0:
        assessment = INCONCLUSIVE
    else:
        assessment = NO_CONCERN
    completeness = sum([have_file, signature_checked, hashed, intel_checked])
    confidence = "High" if completeness >= 4 else ("Medium" if completeness >= 3 else "Low")
    return assessment, confidence


def recommendation(assessment: str, kind: str, can_disable: bool) -> str:
    if assessment == HIGH_CONCERN:
        step = ("Do not open it. Run a full scan in Windows Security (Virus & threat protection → Scan options → "
                "Full scan, or Microsoft Defender Offline scan).")
        if kind == "startup":
            step += (" You can disable this startup entry (I'll ask before changing anything, and it can be restored)."
                     if can_disable else " Disable it in Task Manager → Startup apps or Services after the scan.")
        return step
    if assessment == SUSPICIOUS:
        return ("Find out what installed it: look up the publisher and file name, and check Settings → Apps. If you "
                "don't recognise it, scan it with Windows Security (right-click → Scan with Microsoft Defender)"
                + ("; you can also disable it from starting (reversible)." if kind == "startup" and can_disable else "."))
    if assessment == INCONCLUSIVE:
        return ("Not enough evidence to judge. If the file is missing, the entry is probably left over from an "
                "uninstalled program; otherwise enable a threat-intelligence lookup for a second opinion.")
    return "Nothing needs to be done. Keep Windows and your apps updated."
