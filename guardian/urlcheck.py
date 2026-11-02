"""Phishing and suspicious-URL analysis (defensive, offline by default).

The link is **not opened** for the basic analysis: lookalike domains, misleading subdomains,
IP-address hosts, ``@`` tricks, heavy encoding, shorteners, risky top-level domains and plain HTTP
are judged from the text alone. Following redirects is opt-in ("trace where it goes"): it uses a
safe client (no scripts, no downloads, 6 hops, 6 s each, 64 KB per response) that refuses local,
private-network and cloud-metadata addresses at every hop. Email text is analysed locally; it is
never sent anywhere. No result can prove a link is safe.
"""

from __future__ import annotations

import difflib
import re
import socket
from urllib.parse import unquote, urljoin, urlsplit

from guardian.models import HIGH_CONCERN, NO_CONCERN, SUSPICIOUS, GuardianError, Indicator

BRANDS = {
    "google": "google.com", "gmail": "gmail.com", "youtube": "youtube.com", "microsoft": "microsoft.com",
    "outlook": "outlook.com", "office": "office.com", "live": "live.com", "apple": "apple.com", "icloud": "icloud.com",
    "amazon": "amazon.com", "paypal": "paypal.com", "facebook": "facebook.com", "instagram": "instagram.com",
    "whatsapp": "whatsapp.com", "netflix": "netflix.com", "linkedin": "linkedin.com", "github": "github.com",
    "dropbox": "dropbox.com", "steam": "steampowered.com", "binance": "binance.com", "coinbase": "coinbase.com",
    "flipkart": "flipkart.com", "paytm": "paytm.com", "phonepe": "phonepe.com", "sbi": "onlinesbi.sbi",
    "hdfc": "hdfcbank.com", "icici": "icicibank.com", "axis": "axisbank.com", "irctc": "irctc.co.in",
}
SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "is.gd", "cutt.ly", "rb.gy", "ow.ly", "shorturl.at", "tiny.cc"}
RISKY_TLDS = {"zip", "mov", "xyz", "top", "click", "country", "gq", "tk", "ml", "cf", "ga", "work", "rest", "cam",
              "support", "loan", "win", "bid"}
HOMOGLYPHS = str.maketrans({"0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "@": "a"})
TWO_PART_SUFFIXES = {"co.in", "co.uk", "com.au", "co.jp", "org.uk", "gov.in", "ac.in", "net.in", "com.br"}

URGENCY = re.compile(r"\b(?:urgent|immediately|within 24 hours|account (?:will be )?(?:suspended|locked|closed)|"
                     r"verify your (?:account|identity)|unusual (?:sign-in|activity)|final (?:notice|warning)|"
                     r"act now|limited time)\b", re.I)
CREDENTIALS = re.compile(r"\b(?:password|passcode|otp|one[- ]time (?:password|code)|pin|login details|"
                         r"sign in to (?:confirm|verify)|confirm your (?:details|credentials)|cvv|card number)\b", re.I)
PAYMENT = re.compile(r"\b(?:gift ?cards?|wire transfer|bitcoin|crypto(?:currency)?|upi|pay (?:a|the) (?:fee|fine)|"
                     r"refund|invoice attached|outstanding payment|lottery|prize|you(?:'ve| have) won)\b", re.I)


def registered_domain(host: str) -> str:
    parts = host.lower().strip(".").split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_PART_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def analyse_url(url: str) -> tuple[list[Indicator], dict]:
    raw = url.strip().strip("<>\"'")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.I):
        raw = "http://" + raw
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    facts = {"URL": raw[:300], "Host": host or "—", "Scheme": parts.scheme}
    ind: list[Indicator] = []
    if not host:
        return [Indicator("The address has no valid host name", 2)], facts
    reg = registered_domain(host)
    facts["Registered domain"] = reg
    if host.startswith("xn--") or ".xn--" in host:
        ind.append(Indicator("Uses punycode (international letters that can imitate other letters)", 2))
    if re.fullmatch(r"[\d.]+", host) or ":" in host:
        ind.append(Indicator("Uses a raw IP address instead of a domain name", 2))
    if parts.username:
        ind.append(Indicator("Contains '@' before the host: the part before it is ignored by browsers (a classic "
                             "disguise)", 3))
    if parts.scheme == "http":
        ind.append(Indicator("Not encrypted (http, not https)", 1))
    if reg in SHORTENERS:
        ind.append(Indicator("A link shortener hides the real destination", 1))
    tld = reg.rsplit(".", 1)[-1]
    if tld in RISKY_TLDS:
        ind.append(Indicator(f"Uses the .{tld} ending, which is common in scam sites", 1))
    if raw.count("%") >= 6 or re.search(r"%(?:2e|2f|40)", raw, re.I):
        ind.append(Indicator("Heavily encoded characters in the address", 1))
    if host.count(".") >= 4 or len(host) > 50:
        ind.append(Indicator("Unusually long or deeply nested host name", 1))
    label = reg.split(".")[0]
    normalised = label.translate(HOMOGLYPHS).replace("rn", "m").replace("vv", "w")
    for brand, official in BRANDS.items():
        if reg == official or reg.endswith("." + official):
            facts["Matches official domain"] = official
            ind.append(Indicator(f"Is the official {official} domain", -2))
            break
        if brand in host.replace(reg, "") and reg != official:
            ind.append(Indicator(f"Puts '{brand}' in a subdomain of a different site ({reg}), not on {official}", 3))
            break
        if label == brand:  # amazon.in, google.co.in: the brand's own name with a country ending
            ind.append(Indicator(f"Uses the {brand} name with the .{reg.split('.', 1)[1]} ending (official for some "
                                 f"countries; make sure it's the site you expect)", 0))
            break
        close = difflib.SequenceMatcher(None, normalised, brand).ratio()
        if normalised == brand or (close >= 0.83 and label != brand) or (brand in normalised and label != brand
                                                                                and len(brand) >= 5):
            ind.append(Indicator(f"Looks like '{official}' but is '{reg}' (a lookalike domain)", 3))
            break
    return ind, facts


def analyse_message(text: str) -> tuple[list[Indicator], list[str], dict]:
    """Indicators in email/message text, the links found, and facts. Fully local."""
    ind: list[Indicator] = []
    links = re.findall(r"https?://[^\s<>\"')\]]+", text)
    facts = {"Links found": len(links)}
    if URGENCY.search(text):
        ind.append(Indicator(f"Pressure / urgency: “{URGENCY.search(text).group(0)}”", 1))
    if CREDENTIALS.search(text):
        ind.append(Indicator(f"Asks for login details or codes: “{CREDENTIALS.search(text).group(0)}”", 2))
    if PAYMENT.search(text):
        ind.append(Indicator(f"Talks about payment or prizes: “{PAYMENT.search(text).group(0)}”", 1))
    for m in re.finditer(r"<a[^>]+href=[\"'](https?://[^\"']+)[\"'][^>]*>([^<]+)</a>", text, re.I):
        target, shown = m.group(1), m.group(2)
        shown_host = re.search(r"([a-z0-9-]+\.[a-z.]{2,})", shown.lower())
        if shown_host and registered_domain(shown_host.group(1)) != registered_domain(urlsplit(target).hostname or ""):
            ind.append(Indicator(f"A link shows “{shown.strip()[:40]}” but goes to {urlsplit(target).hostname}", 3))
    sender = re.search(r"^from:\s*\"?([^\"<\n]+?)\"?\s*<([^>]+)>", text, re.I | re.M)
    if sender:
        name, address = sender.group(1).lower(), sender.group(2).lower()
        domain = address.rsplit("@", 1)[-1]
        for brand, official in BRANDS.items():
            if brand in name and registered_domain(domain) != official:
                ind.append(Indicator(f"Sender name says {brand.title()} but the address is @{domain}", 3))
                break
    return ind, links, facts


def judge(indicators: list[Indicator]) -> str:
    score = sum(i.weight for i in indicators)
    if any(i.weight >= 3 for i in indicators) and score >= 4 or score >= 5:
        return HIGH_CONCERN
    if score >= 2 or any(i.weight >= 3 for i in indicators):
        return SUSPICIOUS
    return NO_CONCERN


def trace_redirects(url: str, client_factory=None, is_public=None, max_hops: int = 6) -> list[dict]:
    """Follow redirects safely (no scripts, no downloads, public addresses only)."""
    if is_public is None:
        from research.source_retrieval_service import _public_host

        def is_public(host):
            return _public_host(host, socket.getaddrinfo)
    if client_factory is None:
        import httpx

        def client_factory():
            return httpx.Client(timeout=6.0, follow_redirects=False,
                                headers={"User-Agent": "Mozilla/5.0 (DUMBOO link check)"})
    hops, current = [], url if re.match(r"^https?://", url, re.I) else "http://" + url
    with client_factory() as client:
        for _ in range(max_hops):
            parts = urlsplit(current)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise GuardianError("only http(s) addresses are checked", "invalid")
            if not is_public(parts.hostname):
                hops.append({"url": current, "status": "blocked", "note": "local/private address: not contacted"})
                return hops
            try:
                with client.stream("GET", current) as r:
                    location = r.headers.get("location")
                    size = 0
                    for chunk in r.iter_bytes():
                        size += len(chunk)
                        if size > 65536:
                            break
                    hops.append({"url": current, "status": r.status_code, "type": r.headers.get("content-type", "")})
            except Exception as exc:
                hops.append({"url": current, "status": "error", "note": exc.__class__.__name__})
                return hops
            if r.status_code in (301, 302, 303, 307, 308) and location:
                current = urljoin(current, location)
                continue
            return hops
    hops.append({"url": current, "status": "stopped", "note": f"more than {max_hops} redirects"})
    return hops


__all__ = ["analyse_url", "analyse_message", "judge", "trace_redirects", "registered_domain", "unquote"]
