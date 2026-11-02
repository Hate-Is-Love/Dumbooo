"""Detect secrets and sensitive personal data in free text.

Used before anything is written to long-term memory and before retrieved
memories are put into an AI prompt: passwords, API keys, tokens and similar
login secrets are never stored or sent. Sensitive (but not secret) personal
categories require the user's explicit confirmation before they are saved.
"""

from __future__ import annotations

import re

_SECRET_PATTERNS = [
    re.compile(r"\b(?:password|passwd|passcode|pass\s*word|pin(?:\s*code)?|otp|2fa|one[- ]time code)\b", re.I),
    re.compile(r"\b(?:api[\s_-]?key|secret[\s_-]?key|access[\s_-]?token|auth(?:entication)?[\s_-]?token|"
               r"session[\s_-]?(?:token|id|cookie)|private[\s_-]?key|bearer|recovery (?:code|phrase)|seed phrase)\b",
               re.I),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),                 # Google API keys
    re.compile(r"\bAQ\.[0-9A-Za-z_\-]{20,}"),                  # Google API keys (newer format)
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),                   # OpenAI-style keys
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),               # GitHub tokens
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),                     # card-number-like digit runs
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                      # US SSN pattern
    re.compile(r"\b[A-Za-z0-9+/_\-]{32,}\b"),                  # long random-looking tokens
]

_SENSITIVE_PATTERNS = re.compile(
    r"\b(?:diagnos\w*|disease|illness|medication|medicine|prescription|therapy|therapist|mental health|"
    r"depress\w*|anxiety|pregnan\w*|hiv|cancer|disabilit\w*|religio\w*|church|mosque|temple|"
    r"political|vote[sd]?|party member|sexual\w*|gay|lesbian|bisexual|transgender|ethnic\w*|race|caste|"
    r"bank account|salary|income|debt|criminal|arrest\w*|passport|aadhaar|social security|home address|"
    r"immigration|visa status)\b", re.I)


def contains_secret(text: str) -> bool:
    return any(p.search(text or "") for p in _SECRET_PATTERNS)


def is_sensitive(text: str) -> bool:
    return bool(_SENSITIVE_PATTERNS.search(text or ""))


def redact_secrets(text: str) -> str:
    """Replace secret-looking substrings (used for logs and safe metadata)."""
    out = text or ""
    for pattern in _SECRET_PATTERNS[2:]:
        out = pattern.sub("[REDACTED]", out)
    return out
