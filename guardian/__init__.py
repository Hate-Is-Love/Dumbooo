"""Module 7: AI Cybersecurity & Privacy Guardian.

Real, locally executed, read-first security checks for *this* computer: security overview,
startup-entry investigation (paths, signatures, hashes, optional threat intelligence),
processes, network connections, software updates and advisories, URL/phishing analysis,
file-integrity monitoring, and a small set of confirmed, audited, reversible actions.

Self-contained package: it plugs into DUMBOO through one conversation hook
(``guardian.assistant.GuardianAssistant``), one dashboard page, one database migration (v6) and
the ``security`` settings section. Other modules are not changed.

Evidence rules (enforced in code, not only in prompts): every check returns an explicit status
(PASS / WARNING / FAIL / UNKNOWN / NOT_SUPPORTED); a failed or denied check is UNKNOWN, never
proof that something is disabled; nothing is called malicious without real evidence, and the
AI only explains structured results.
"""
