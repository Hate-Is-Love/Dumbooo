"""Casual and slang phrasing -> the plain commands the automation rules understand.

"yo pull up yt real quick", "fire up notepad", "youtube kholo", "whats my battery at",
"play lofi on youtube": this module rewrites such requests into "open youtube",
"open notepad", "check battery", "open https://www.youtube.com/results?search_query=lofi"
before ``automation.intents`` parses them.

Rules of thumb (so normal conversation is never hijacked):

* A casual verb ("pull up", "fire up", "gimme", "take me to", "kholo" …) is only turned
  into "open X" when X is a known website or approved app (or alias of one).
* A bare name ("youtube", "spotify") only becomes "open X" when it is exactly a known
  site or app.
* Text to be typed ("… and type <text>") is never changed.
"""

from __future__ import annotations

import re
from typing import Callable
from urllib.parse import quote_plus

# Short names people use for sites and apps.
ALIASES = {
    "yt": "youtube", "you tube": "youtube", "utube": "youtube", "yt music": "youtube music",
    "insta": "instagram", "ig": "instagram", "the gram": "instagram", "gram": "instagram",
    "fb": "facebook", "twitter": "x", "x.com": "x", "tweeter": "x",
    "wa": "whatsapp", "whatsapp web": "whatsapp", "whats app": "whatsapp",
    "gpt": "chatgpt", "chat gpt": "chatgpt", "mail": "gmail", "g mail": "gmail", "email": "gmail",
    "maps": "google maps", "gmaps": "google maps", "drive": "google drive", "gdrive": "google drive",
    "so": "stack overflow", "stackoverflow": "stack overflow", "wiki": "wikipedia",
    "calc": "calculator", "vs": "vs code", "vsc": "vs code", "code": "vs code",
    "browser": "chrome", "files": "file explorer", "my computer": "file explorer", "this pc": "file explorer",
}

_FILLER_LEAD = re.compile(r"^(?:(?:yo|yoo|bro|bruh|dude|man|hey|hi|ok|okay|so|um+|uh+|aye|oi|listen|"
                          r"alright|right)[\s,!]+)+", re.I)
_FILLER_TAIL = re.compile(r"(?:[\s,]+(?:real quick|quickly|quick|rn|right now|asap|now|pls|plz|please|bro|bruh|dude|"
                          r"man|mate|for me|yaar|na|thanks|thx|ty|ok|okay))+[\s.!?]*$", re.I)
_TEXTSPEAK = [(r"\bu\b", "you"), (r"\bur\b", "your"), (r"\bpls\b|\bplz\b", "please"), (r"\bwhats\b", "what's"),
              (r"\bhows\b", "how's"), (r"\bwanna\b", "want to"), (r"\bgonna\b", "going to"), (r"\bya\b", "you"),
              (r"\bcan you\b|\bcould you\b|\bwould you\b|\bwill you\b", "")]
_OPEN_VERBS = (r"pull up|bring up|fire up|boot up|load up|open up|start up|spin up|put on|throw on|hop on|hop onto|"
               r"get on|jump on|jump onto|gimme|give me|get me|take me to|go to|go on|head to|head over to|"
               r"navigate to|visit|show me|i want|i wanna see|let's see|lemme see|open|launch|start|run")
_CLOSE_VERBS = r"shut|shut down|exit|quit|close out|close out of|x out of|get rid of|close"
_HINDI_OPEN = r"(?:kholo|khol do|khol|chalu karo|chalu kar do|start karo|open karo|open kar do|lagao|laga do)"
_HINDI_CLOSE = r"(?:band karo|band kar do|band kar|bandh karo|close karo|hatao)"
_MEDIA_SITES = {
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "spotify": "https://open.spotify.com/search/{q}",
    "youtube music": "https://music.youtube.com/search?q={q}",
}


def _alias(name: str) -> str:
    n = " ".join(name.lower().strip(" .!?,'\"").split())
    n = re.sub(r"^(?:the|my)\s+", "", n)
    n = re.sub(r"\s+(?:app|application|website|site|web ?site|page)$", "", n)
    return ALIASES.get(n, n)


def normalize(text: str, known: Callable[[str], bool]) -> str:
    """Rewrite slang into a plain command. ``known(name)`` says if a site/app name exists."""
    raw = (text or "").strip()
    if not raw:
        return raw
    # Never touch text that is meant to be typed.
    m = re.match(r"^(?P<head>.*?)(?P<tail>\b(?:and (?:then )?)?(?:type|write)\b.*)$", raw, re.I | re.S)
    head, tail = (m.group("head"), m.group("tail")) if m else (raw, "")
    t = head
    for pattern, repl in _TEXTSPEAK:
        t = re.sub(pattern, repl, t, flags=re.I)
    t = " ".join(t.split())
    t = _FILLER_LEAD.sub("", t)
    if not tail:
        t = _FILLER_TAIL.sub("", t)
    t = t.strip(" ,")

    # "play <query> on youtube" / "search youtube for <query>" / "youtube <query>"
    mm = (re.match(r"^(?:play|put on|stream|search|find|look up|watch)\s+(?P<q>.+?)\s+on\s+(?P<site>youtube|yt|spotify|"
                   r"youtube music|yt music)$", t, re.I)
          or re.match(r"^(?:search|look up)\s+(?P<site>youtube|yt|spotify)\s+for\s+(?P<q>.+)$", t, re.I))
    if mm and not tail:
        site = _alias(mm.group("site"))
        return f"open {_MEDIA_SITES[site].format(q=quote_plus(mm.group('q').strip()))}"

    # Hindi: "youtube kholo", "notepad band karo"
    hm = re.match(rf"^(?P<x>.+?)\s+{_HINDI_OPEN}$", t, re.I)
    if hm and known(_alias(hm.group("x"))):
        return f"open {_alias(hm.group('x'))}" + (" " + tail if tail else "")
    hm = re.match(rf"^(?P<x>.+?)\s+{_HINDI_CLOSE}$", t, re.I)
    if hm and known(_alias(hm.group("x"))):
        return f"close {_alias(hm.group('x'))}"

    # Casual "open" verbs, only for known sites/apps.
    om = re.match(rf"^(?:{_OPEN_VERBS})\s+(?:up\s+)?(?P<x>.+?)$", t, re.I)
    if om and known(_alias(om.group("x"))):
        return f"open {_alias(om.group('x'))}" + (" " + tail if tail else "")
    cm = re.match(rf"^(?:{_CLOSE_VERBS})\s+(?P<x>.+?)$", t, re.I)
    if cm and known(_alias(cm.group("x"))) and not re.match(r"^shut down$", t, re.I):
        return f"close {_alias(cm.group('x'))}"

    # A bare known name: "youtube", "spotify pls"
    if not tail and known(_alias(t)):
        return f"open {_alias(t)}"

    # Everyday phrasings of system checks and screenshots.
    simple = [
        (r"^(?:what's|how's|hows)?\s*(?:my |the )?battery(?: at| like| level| looking| percent(?:age)?)?\??$|"
         r"^how much battery(?: (?:is )?left| do i have)?\??$|^battery\??$", "check battery"),
        (r"^(?:how's|hows|what's) (?:my |the )?(?:ram|memory)(?: (?:at|like|looking|usage))?\??$|^ram\??$",
         "how much ram is available"),
        (r"^(?:how's|hows|what's) (?:my |the )?cpu(?: (?:at|like|looking|usage|doing))?\??$", "check cpu usage"),
        (r"^(?:how's|hows) (?:my |the )?(?:laptop|pc|computer|system)(?: doing)?\??$", "check my laptop"),
        (r"^(?:screenshot(?: this| that| it)?|snap (?:the |my )?screen|ss|grab (?:the |my )?screen|"
         r"take (?:a )?(?:pic|picture|snap) of (?:the |my )?screen)$", "take a screenshot"),
        (r"^(?:how much )?(?:storage|space|disk)(?: (?:left|free))?\??$|^am i (?:running )?out of (?:space|storage)\??$",
         "check disk space"),
    ]
    for pattern, plain in simple:
        if not tail and re.match(pattern, t, re.I):
            return plain
    return (t + (" " + tail if tail else "")).strip() if t != head.strip() else raw
