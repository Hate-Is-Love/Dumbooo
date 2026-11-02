"""Deciding when a message needs live web research (and of which kind).

Ordinary conversation ("tell me a joke", "explain recursion") is *not* researched. Live
research is used for explicit requests (search, look up, research, verify, news …) and,
when ``auto_research`` is on, for questions that depend on current information
("what is the latest version of …", "who is the current …", "… today").
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from automation.intents import ORDINALS, clean

NEWS_WORDS = r"(?:news|headlines|developments|updates|happenings|stories|announcements)"
RESEARCH_NOUNS = re.compile(r"\b(explanation|explained|article|articles|guide|tutorial|documentation|docs|requirements|"
                            r"information|info|definition|meaning|official|website|resources?|sources?|papers?|"
                            r"studies|statistics|stats|data|reviews?|comparison|best|latest|news|updates?|price|"
                            r"eligibility|fees?|syllabus|release|version)\b", re.I)
TIME_SENSITIVE = re.compile(r"\b(latest|newest|current|currently|today|tonight|right now|this (?:week|month|year)|"
                            r"recent|recently|as of|new version|latest version|release date|released|price of|"
                            r"stock price|exchange rate|score|weather|forecast|who won|who is the (?:current|new)|"
                            r"is it true|still)\b|\b20(?:2[4-9]|3\d)\b", re.I)
QUESTION = re.compile(r"^(?:what|who|when|where|which|how|is|are|was|were|does|do|did|has|have|can|will|should)\b|\?$",
                      re.I)
REFERENT = re.compile(r"\b(this|these|that|those)\s+(course|courses|library|libraries|topic|event|colleges?|claim|"
                      r"report|news report|story|model|models|tool|tools|product|company|framework|subject)\b", re.I)


@dataclass
class ResearchIntent:
    kind: str                # search | news | research | factcheck | open_docs | history | open_saved | refresh |
                             # continue | compare_previous | save | use_saved | followup | sources | open_source | export
    query: str = ""
    days: int | None = None
    index: int | None = None  # follow-ups: "the second development" -> 2
    referent: str = ""        # "this course" etc. that must be resolved from context
    official: bool = False    # prefer official/primary sources
    compare: bool = False


def _ordinal(text: str) -> int | None:
    m = re.search(r"\b(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|top|\d{1,2}(?:st|nd|rd|th)?)\b", text, re.I)
    if not m:
        return None
    w = m.group(1).lower()
    w2 = re.sub(r"(st|nd|rd|th)$", "", w)
    return ORDINALS.get(w) or (int(w2) if w2.isdigit() else None)


def _topic(text: str) -> str:
    t = re.sub(r"^(?:please\s+)?(?:can you\s+|could you\s+)?", "", text.strip(), flags=re.I)
    return t.strip(" ?.!\"'“”")


def _referent(text: str) -> str:
    m = REFERENT.search(text)
    return m.group(0) if m else ""


def classify(text: str, *, has_last: bool = False, auto_research: bool = True) -> ResearchIntent | None:
    t = clean(text)
    if not t:
        return None
    low = t.lower()

    # -- history, saved findings and follow-ups (no new search unless needed) -------------------
    if re.match(r"^(?:show|list|find|search)(?: me)? (?:my |all my )?(?:previous |past |earlier |old |saved )*"
                r"research(?:es)?(?: (?:about|on|for) (?P<q>.+))?$", t, re.I):
        m = re.match(r".*?(?:about|on|for) (?P<q>.+)$", t, re.I)
        return ResearchIntent("history", m.group("q") if m else "")
    if re.match(r"^open (?:the |my )?(?:saved |previous |last )?research(?: report)?(?: (?:i|we) (?:saved|did))?"
                r"(?: (?:about|on) (?P<q>.+))?$", t, re.I):
        m = re.match(r".*?(?:about|on) (?P<q>.+)$", t, re.I)
        return ResearchIntent("open_saved", m.group("q") if m else "")
    if re.match(r"^(?:refresh|update|redo|re-run|rerun) (?:my |the )?(?:previous |last |earlier |saved )?research"
                r"(?: (?:about|on) (?P<q>.+?))?(?: with (?:current|new|fresh|latest) sources)?$", t, re.I):
        m = re.match(r".*?research (?:about|on) (?P<q>.+?)(?: with (?:current|new|fresh|latest) sources)?$", t, re.I)
        return ResearchIntent("refresh", m.group("q") if m else "")
    if re.match(r"^(?:continue|resume|keep going with|pick up) (?:the |my |our )?research(?: (?:we|i|you) did)?"
                r"(?: (?:yesterday|earlier|before|last time|today))?$", t, re.I):
        return ResearchIntent("continue")
    if re.match(r"^compare (?:this|it|that) (?:with|to) (?:the |my )?(?:previous|earlier|last|old) research"
                r"|^compare (?:this|it|that) (?:with|to) the research (?:i|we) (?:asked|did)", t, re.I):
        return ResearchIntent("compare_previous")
    if re.match(r"^(?:remember|save|store|keep)(?: these| this| the)? (?:findings|research|results|sources|report)"
                r"(?: (?:for|to|in) (?:my )?(?P<p>.+?))?$", t, re.I):
        m = re.match(r".*?(?:for|to|in) (?:my )?(?P<p>.+?)$", t, re.I)
        return ResearchIntent("save", m.group("p") if m else "")
    if re.match(r"^(?:use|search|check) (?:the |my )?(?:sources|links) (?:i|we) saved(?: earlier| before)?"
                r"(?: (?:for|about|to answer) (?P<q>.+))?$", t, re.I):
        m = re.match(r".*?(?:for|about|to answer) (?P<q>.+)$", t, re.I)
        return ResearchIntent("use_saved", m.group("q") if m else "")
    if re.match(r"^(?:export|save) (?:this|the|that) (?:research )?(?:report|research)(?: as (?:a )?(?:file|markdown))?$",
                t, re.I):
        return ResearchIntent("export")
    if has_last:
        if re.match(r"^(?:show|give|list|what are)(?: me)? (?:the )?(?:sources|links|references|citations)$", t, re.I):
            return ResearchIntent("sources")
        m = re.match(r"^open (?:the )?(?P<n>\w+) (?:source|link|article|result|reference)$|"
                     r"^open (?:source|link|result|reference) (?:number )?(?P<n2>\d+)$", t, re.I)
        if m:
            return ResearchIntent("open_source", index=_ordinal(m.group("n") or m.group("n2") or "1"))
        if re.match(r"^(?:explain|tell me (?:more )?about|expand on|more (?:details? )?(?:about|on)|go deeper (?:into|on)|"
                    r"what about) (?:the )?\w+ (?:development|point|finding|item|story|result|update|one|claim)"
                    r"(?: in (?:more )?detail)?$", t, re.I) or re.match(r"^(?:more detail|more details|tell me more|"
                                                                          r"elaborate|go deeper)(?: please)?$", t, re.I):
            return ResearchIntent("followup", index=_ordinal(t) or 1)

    # -- fact-checking ------------------------------------------------------------------------------
    m = re.match(r"^(?:please )?(?:verify|fact[- ]?check|double[- ]check|check)(?: (?:whether|if|that))?"
                 r"(?: (?:this|the|that) (?:information|info|claim|statement|answer|report|news report|news))?"
                 r"(?: is (?:correct|true|right|accurate))?(?: (?:using|with|against) (?:reliable|official|"
                 r"independent|other|trusted)? ?sources)?\s*[:,-]?\s*(?P<claim>.*)$", t, re.I | re.S)
    if m and re.match(r"^(?:please )?(?:verify|fact[- ]?check|double[- ]check|check (?:whether|if|that|this|the claim|"
                      r"this claim))", t, re.I):
        claim = m.group("claim").strip(" \"'“”")
        if re.match(r"^the official requirements before answering$", claim, re.I):
            return ResearchIntent("search", "", official=True, referent="previous question")
        return ResearchIntent("factcheck", claim, referent="" if claim else "previous answer")
    m = re.match(r"^is (?:it|this|that) true that (?P<claim>.+)$", t, re.I)
    if m:
        return ResearchIntent("factcheck", m.group("claim"))
    if re.match(r"^is (?:this|that|the) (?:news )?(?:report|story|claim|news) (?:supported|confirmed|backed)"
                r" by (?:other|independent|multiple) sources$", t, re.I):
        return ResearchIntent("factcheck", "", referent="previous answer")

    # -- open the official documentation ---------------------------------------------------------------
    m = re.match(r"^(?:open|show me|take me to) (?:the )?official (?:documentation|docs|website|site|page)"
                 r"(?: (?:for|of) (?P<q>.+))?$", t, re.I)
    if m:
        q = (m.group("q") or "").strip()
        return ResearchIntent("open_docs", q, official=True, referent=_referent(q) if q else "this library")

    # -- news ---------------------------------------------------------------------------------------------
    from research.news_service import window_days

    bare = re.match(rf"^(?:the )?(?:latest|recent|newest|today'?s|this week'?s|top|current)"
                    rf"(?: (?:important|main|top|big|key|tech))*(?: (?P<pre>[\w .&+-]{{1,40}}?))? {NEWS_WORDS}"
                    rf"(?: (?:about|on|in|from|for|regarding) (?P<post>.+?))?(?: (?:today|this week|this month|"
                    rf"recently|right now))?$", t, re.I)
    if bare:
        topic = (bare.group("post") or bare.group("pre") or "").strip() or "top news"
        return ResearchIntent("news", topic, days=window_days(t, 0) or None, referent=_referent(topic))
    m = re.match(rf"^(?:what are|show me|give me|tell me|summari[sz]e|get|find|any)(?: me)?(?: the)?(?: some)?"
                 rf"(?: (?:latest|recent|newest|today'?s|this week'?s|current|top|important|main|big|key))*"
                 rf"(?: (?P<pre>[\w .&+-]{{1,40}}?))? {NEWS_WORDS}(?: (?:about|on|in|from|for|regarding) "
                 rf"(?P<post>.+?))?(?: (?:today|this week|this month|recently|right now))?$", t, re.I)
    if m and (m.group("pre") or m.group("post")) and not re.search(r"\bmy (?:files?|folders?|downloads)\b", low):
        topic = (m.group("post") or m.group("pre") or "").strip()
        topic = re.sub(r"^(?:the |important |main |top |latest |recent )+", "", topic, flags=re.I)
        return ResearchIntent("news", topic, days=window_days(t, 0) or None,
                              referent=_referent(topic))
    m = re.match(r"^what(?:'s| has| is)? happen(?:ed|ing)(?: recently| lately| today| this week)? (?:in|with|to|"
                 r"around) (?P<q>.+?)(?: recently| lately| today| this week)?$", t, re.I)
    if m:
        return ResearchIntent("news", m.group("q"), days=window_days(t, 0) or None)
    m = re.match(r"^compare how (?:different |various |other )?(?:sources|outlets|media|news sites|publications) "
                 r"(?:are |were )?(?:reporting|covering|describing) (?P<q>.+)$", t, re.I)
    if m:
        q = m.group("q")
        return ResearchIntent("news", q, compare=True, referent=_referent(q))
    if re.match(rf"^(?:find|search for|look up) (?:the )?(?:latest|recent|newest) {NEWS_WORDS} (?:about|on|in) (?P<q>.+)$",
                t, re.I):
        q = re.match(r".*? (?:about|on|in) (?P<q>.+)$", t, re.I).group("q")
        return ResearchIntent("news", q)

    # -- deep research ------------------------------------------------------------------------------------
    m = re.match(r"^(?:research|investigate|deep[- ]dive(?: into)?|do (?:a )?(?:deep |detailed )?research (?:on|into|about))"
                 r" (?P<q>.+)$", t, re.I)
    if m:
        q = re.sub(r"\s+and (?:give|write|make) me (?:a |an )?(?:detailed |full |short )?report(?: with references)?$",
                   "", m.group("q"), flags=re.I)
        return ResearchIntent("research", q, referent=_referent(q))
    m = re.match(r"^compare (?P<q>.+?)(?: using| with| based on| from) (?:current|the latest|up-to-date|official|recent|"
                 r"reliable)(?: information| data| sources| info)?$", t, re.I)
    if m:
        return ResearchIntent("research", "Compare " + m.group("q"), official="official" in low,
                              referent=_referent(m.group("q")))
    if re.search(r"\b(?:detailed report|full report|with references|in-depth analysis)\b", low) and \
            re.match(r"^(?:give|write|make|prepare|create) ", t, re.I):
        return ResearchIntent("research", t, referent=_referent(t))

    # -- explicit web search --------------------------------------------------------------------------------
    m = re.match(r"^(?:search|google|look up|lookup|bing|search the web|search online|search the internet)"
                 r"(?: (?:the web|online|the internet))?(?: for| about)? (?P<q>.+)$", t, re.I)
    if m and not re.search(r"\b(?:my (?:computer|laptop|pc|files?|folders?|downloads|documents|desktop)|in (?:my )?"
                           r"(?:downloads|documents|desktop))\b", low) and not re.search(r"\bin (?:my |dumboo'?s )?browser\b",
                                                                                         low):
        q = m.group("q")
        return ResearchIntent("search", q, official="official" in q.lower(), referent=_referent(q))
    m = re.match(r"^find (?:me )?(?:the |a |an |some )?(?P<q>.+)$", t, re.I)
    if m and RESEARCH_NOUNS.search(m.group("q")) and not re.match(r"^(?:my|these|those)\b", m.group("q"), re.I) \
            and not re.search(r"\b(?:files?|folders?|pdfs?|readme|project folder|downloads|duplicates?)\b", low):
        q = m.group("q")
        if re.search(rf"\b(?:latest|recent) {NEWS_WORDS}\b", q, re.I):
            topic = re.sub(rf"^.*?\b(?:latest|recent) {NEWS_WORDS} (?:about|on|in)?\s*", "", q, flags=re.I) or q
            return ResearchIntent("news", topic)
        return ResearchIntent("search", q, official="official" in q.lower(), referent=_referent(q))

    # -- questions that need current information --------------------------------------------------------------
    if auto_research and QUESTION.search(t) and TIME_SENSITIVE.search(t) and len(t) >= 12 and \
            not re.search(r"\b(?:you|your|dumboo)\b", low) and not re.search(r"\bmy (?:laptop|computer|pc|ram|disk|"
                                                                             r"battery|cpu|files?)\b", low):
        return ResearchIntent("search", _topic(t))
    return None
