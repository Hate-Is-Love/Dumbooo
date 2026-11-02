"""Readable reports (Markdown for the screen, short sentences for speech) from structured results.

Speech never contains IP addresses, file hashes or full paths (privacy and readability).
"""

from __future__ import annotations

from guardian.models import FAIL, NOT_SUPPORTED, PASS, UNKNOWN, WARNING, Investigation

ICON = {PASS: "✅", WARNING: "⚠️", FAIL: "❌", UNKNOWN: "❔", NOT_SUPPORTED: "➖"}
ASSESS_ICON = {"No obvious concern": "✅", "Suspicious indicators": "⚠️", "High concern": "❌", "Inconclusive": "❔"}


def overview_markdown(result: dict) -> str:
    lines = [f"**Security check** · {result['time'].replace('T', ' ')}", ""]
    for c in result["checks"]:
        lines.append(f"{ICON.get(c['status'], '•')} **{c['title']}**: {c['detail']}" +
                     (f" — _{c['advice']}_" if c.get("advice") else ""))
    flagged = result.get("flagged_startup") or []
    if flagged:
        lines += ["", "**Startup entries to look at:** " + ", ".join(flagged[:8]) +
                  " — say “investigate the startup entry …”."]
    lines += ["", "_UNKNOWN means DUMBOO couldn't read that setting (often it needs administrator rights) — not that "
              "it is off._"]
    return "\n\n".join(line for line in lines if line != "")


def overview_speech(result: dict, title: str) -> str:
    counts = result["counts"]
    problems = [c for c in result["checks"] if c["status"] in (FAIL, WARNING)]
    who = f", {title}" if title else ""
    if not problems:
        text = f"The security check is complete{who}. Everything I could check looks fine."
    else:
        parts = []
        for c in problems[:3]:
            if c["id"] == "software_updates":
                n = len(c["evidence"].get("updates", []))
                parts.append(f"{n} app{'s' if n != 1 else ''} with available updates")
            elif c["id"] == "startup":
                n = len(c["evidence"].get("flagged", []))
                parts.append(f"{n} startup entr{'ies' if n != 1 else 'y'} that need{'' if n != 1 else 's'} a closer look")
            else:
                parts.append(f"{c['title'].lower()}: {c['detail'].lower()}")
        text = f"The security check is complete{who}. I found " + _join(parts) + "."
    if counts.get(UNKNOWN):
        text += f" {counts[UNKNOWN]} item{'s' if counts[UNKNOWN] != 1 else ''} couldn't be checked without administrator rights."
    if problems:
        text += " Would you like to review the findings? The details are on screen."
    return text


def investigation_markdown(inv: Investigation) -> str:
    lines = [f"**Investigation: {inv.subject}**", ""]
    for key, value in inv.facts.items():
        lines.append(f"- **{key}:** {value}")
    intel = inv.intel
    if intel.get("status") in ("NOT_CHECKED", "ERROR"):
        intel_text = f"NOT_CHECKED ({intel.get('reason')})" if intel.get("status") == "NOT_CHECKED" else \
            f"ERROR ({intel.get('reason')})"
    else:
        intel_text = f"{intel.get('provider')}: {intel.get('status')}" + (
            f" — {intel.get('malicious')}/{intel.get('engines')} engines" if "engines" in intel else "") + (
            f" · {intel['link']}" if intel.get("link") else "")
    lines.append(f"- **Threat intelligence:** {intel_text}")
    lines += ["", "**Findings**"]
    findings = [i for i in inv.indicators if i.weight != 0] or [i for i in inv.indicators]
    if not findings:
        lines.append("- Nothing unusual was observed.")
    for i in findings:
        tag = {"external": "external intelligence", "inferred": "inference"}.get(i.kind, "observed")
        mark = "🔴" if i.weight >= 3 else ("🟠" if i.weight > 0 else ("🟢" if i.weight < 0 else "⚪"))
        lines.append(f"- {mark} {i.text} _({tag})_")
    lines += ["", f"**Assessment:** {ASSESS_ICON.get(inv.assessment, '')} {inv.assessment} · **Confidence:** "
                  f"{inv.confidence}", "", f"**Recommended action:** {inv.recommendation}", "", "**Limitations**"]
    lines += [f"- {l}" for l in inv.limitations]
    return "\n".join(lines)


def investigation_speech(inv: Investigation) -> str:
    publisher = inv.facts.get("Publisher", "UNKNOWN")
    sig = inv.facts.get("Signature", "not checked")
    who = f"signed by {publisher}" if sig == "Valid" else ("not digitally signed" if sig == "NotSigned"
                                                           else "its signature couldn't be verified")
    concerns = [i.text for i in inv.indicators if i.weight >= 2]
    text = f"{inv.subject}: {inv.assessment.lower()}, {inv.confidence.lower()} confidence. It's {who}."
    if concerns:
        text += " Main concern: " + concerns[0].rstrip(".") + "."
    if inv.facts.get("File exists") == "no":
        text += " The file it points to doesn't exist."
    return text + " The full report is on screen. I won't disable or delete anything without your approval."


def network_markdown(n: dict) -> str:
    lines = ["**Network overview**", ""]
    for i in n["interfaces"]:
        lines.append(f"- 🔌 **{i['name']}**: {', '.join(i['addresses'])}" +
                     (f" ({i['speed_mbps']} Mbit/s)" if i.get("speed_mbps") else ""))
    lines += ["", f"**{n['total_connections']} active connections** from {n['programs']} programs "
                  f"(showing up to {len(n['connections'])}):", "",
              "| Program | Remote address | Port | State |", "|---|---|---|---|"]
    if n.get("loopback"):
        lines[-2:-2] = [f"_{n['loopback']} of them are between programs on this PC (listed last)._", ""]
    for c in n["connections"][:40]:
        where = " (public)" if c["public"] else (" (this PC)" if c["remote"].startswith(("127.", "::1")) else
                                                  " (local network)")
        lines.append(f"| {c['program']} | {c['remote']}{where} | "
                     f"{c['remote_port']} | {c['state']}{' ⚠️ ' + c['note'] if c.get('note') else ''} |")
    if n["exposed"]:
        lines += ["", "**Listening on all network interfaces** (reachable from other devices if the firewall allows): "
                  + ", ".join(f"{p} :{port}" for p, port in n["exposed"][:15])]
    for note in n["notes"]:
        lines += ["", f"⚠️ {note}"]
    lines += ["", "_An unfamiliar address is not proof of anything; many apps talk to cloud servers._"]
    return "\n".join(lines)


def network_speech(n: dict) -> str:
    text = (f"There are {n['total_connections']} active connections from {n['programs']} programs, and "
            f"{len(n['exposed'])} programs listening for incoming connections.")
    if n["notes"]:
        text += f" {len(n['notes'])} connection{'s use' if len(n['notes']) != 1 else ' uses'} an unusual port."
    return text + " The details are on screen."


def updates_markdown(u: dict) -> str:
    lines = [f"**Software updates** · checked {u['checked'].replace('T', ' ')}", ""]
    if u["updates"]:
        lines += ["| App | Installed | Available | Source |", "|---|---|---|---|"]
        lines += [f"| {x['name']} | {x['installed']} | {x['available']} | {x['source']} |" for x in u["updates"]]
        lines += ["", "_An available update is not necessarily a security fix. Update from the app itself or with "
                  "winget upgrade followed by the app ID; DUMBOO doesn't install anything._"]
    else:
        lines.append("No updates listed.")
    if u["advisories"]:
        cat = u.get("catalogue", {})
        lines += ["", f"**Security advisories to review** ({cat.get('name')}, version {cat.get('version')}):", ""]
        for a in u["advisories"]:
            lines.append(f"- **{a['app']}** ({a['installed']}): [{a['cve']}]({a['link']}) — {a['title']} "
                         f"(added {a['added']}). _Your version was not verified against the affected range._")
    for note in u["notes"]:
        lines += ["", f"ℹ {note}"]
    return "\n".join(lines)


def url_markdown(r: dict) -> str:
    lines = [f"**Link check:** {ASSESS_ICON.get(r['assessment'], '')} {r['assessment']}", ""]
    for k, v in r["facts"].items():
        lines.append(f"- **{k}:** {v}")
    lines += ["", "**Indicators**"]
    lines += [f"- {'🔴' if i['weight'] >= 3 else ('🟠' if i['weight'] > 0 else '🟢')} {i['text']}" for i in r["indicators"]] \
        or ["- No warning signs in the address itself."]
    if r.get("hops"):
        lines += ["", "**Where it leads** (checked safely, no scripts run):"]
        lines += [f"{n + 1}. {h['url']} → {h['status']}" for n, h in enumerate(r["hops"])]
    elif not r.get("opened"):
        lines += ["", "_The link was not opened. Say “trace where this link goes” to follow its redirects safely._"]
    lines += ["", "_No check can prove a link is safe. Never enter passwords after following a link from a message._"]
    return "\n".join(lines)


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
