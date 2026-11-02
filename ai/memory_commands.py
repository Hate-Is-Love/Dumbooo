"""Recognise memory commands and personal facts in what the user says.

This is deliberately rule-based (no AI involved): what gets stored, updated or
deleted is decided by these predictable rules, never by a model.

Explicit commands (handled locally, no AI call):
  "remember (that) my project is called DUMBOO"     -> save / update a fact
  "actually, my project is called Nova"              -> correction (supersedes)
  "update my saved information: my city is Pune"     -> correction
  "what do you remember about me" / "show my saved memories"
  "forget my favourite colour" / "forget that" / "delete this memory"
  "clear my memories" / "forget everything"          -> asks for confirmation
  "call me Captain"                                  -> sets the title DUMBOO uses

Automatic extraction (only clear, stable first-person statements, never
questions): my name is …, my X is called …, my favourite X is …, I live in …,
I work as/at …, I'm working on …, my birthday is …
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_LEAD = re.compile(r"^\s*(?:(?:hey|hi|ok|okay)\s+)?(?:dumboo|dumbo)\s*[,:]?\s*", re.I)
_TRAIL = re.compile(r"[\s.!?;]+$")


@dataclass
class Command:
    kind: str            # confirm | decline | list | clear_all | forget | remember | correct | set_title | none
    arg: str = ""


@dataclass
class ParsedFact:
    key: str
    value: str
    category: str
    structured: bool

    def spoken(self) -> str:
        """How DUMBOO says the fact back: 'your project is called DUMBOO'."""
        if not self.structured:
            return self.value
        if self.key == "name":
            return f"your name is {self.value}"
        if self.key in ("current project",):
            return f"you're working on {self.value}"
        if self.key == "home":
            return f"you live in {self.value}"
        if self.key == "job":
            return f"you work as {self.value}"
        if self.key == "workplace":
            return f"you work at {self.value}"
        if self.key == "age":
            return f"you are {self.value} years old"
        verb = "is called" if self.category == "project" or "name" in self.key else "is"
        return f"your {self.key} {verb} {self.value}"


def clean(text: str) -> str:
    return _TRAIL.sub("", _LEAD.sub("", text or "")).strip()


def _strip_value(value: str) -> str:
    value = value.strip().strip("\"'“”‘’")
    return _TRAIL.sub("", value).strip()


def _category_for(key: str, value: str = "") -> str:
    k = key.lower()
    if "project" in k or "app" in k or "startup" in k:
        return "project"
    if any(w in k for w in ("goal", "plan", "aim", "target", "dream")):
        return "goal"
    if any(w in k for w in ("name", "age", "birthday", "home", "city", "job", "workplace", "nationality", "language")):
        return "identity"
    if any(w in k for w in ("favourite", "favorite", "prefer", "like")):
        return "preference"
    if re.match(r"^(always|never|whenever|please|don't|do not)\b", value.lower()):
        return "instruction"
    return "fact"


_FACT_PATTERNS: list[tuple[re.Pattern, str | None]] = [
    (re.compile(r"^my (?P<key>[\w\s'-]{1,40}?) (?:is|are) (?:called|named) (?P<val>.+)$", re.I), None),
    (re.compile(r"^my name is (?P<val>.+)$", re.I), "name"),
    (re.compile(r"^my (?P<key>favou?rite [\w\s-]{1,30}?) (?:is|are) (?P<val>.+)$", re.I), None),
    (re.compile(r"^my birthday is (?:on )?(?P<val>.+)$", re.I), "birthday"),
    (re.compile(r"^my (?P<key>[\w\s'-]{1,40}?) (?:is|are|=) (?P<val>.+)$", re.I), None),
    (re.compile(r"^i(?:'m| am) (?P<val>\d{1,3}) years? old$", re.I), "age"),
    (re.compile(r"^i (?:live|stay) in (?P<val>.+)$", re.I), "home"),
    (re.compile(r"^i work as (?:an? )?(?P<val>.+)$", re.I), "job"),
    (re.compile(r"^i work (?:at|for) (?P<val>.+)$", re.I), "workplace"),
    (re.compile(r"^i(?:'m| am) (?:currently )?working on (?P<val>.+)$", re.I), "current project"),
]

# Auto-extraction uses only the strongest, stable patterns.
_AUTO_KEYS_ALLOWED = {"name", "birthday", "home", "job", "workplace", "current project", "age"}


def parse_fact(fact: str) -> ParsedFact:
    fact = clean(fact)
    fact = re.sub(r"^(?:that|this)\s*:?\s+", "", fact, flags=re.I)
    for pattern, fixed_key in _FACT_PATTERNS:
        match = pattern.match(fact)
        if not match:
            continue
        key = fixed_key or " ".join(match.group("key").lower().split())
        value = _strip_value(match.group("val"))
        if key and value and len(value) <= 200:
            key = re.sub(r"\bfavorite\b", "favourite", key)
            return ParsedFact(key, value, _category_for(key, value), True)
    # Not a recognisable "my X is Y" sentence: store the whole statement.
    value = _strip_value(fact)
    words = re.findall(r"[A-Za-z0-9']+", value.lower())
    key = " ".join(words[:8]) or value[:60]
    return ParsedFact(key, value, _category_for(key, value), False)


_CONFIRM = re.compile(r"^(?:yes|yeah|yep|yup|confirm(?:ed)?|sure|ok(?:ay)?|do it|go ahead|please do)\b", re.I)
_DECLINE = re.compile(r"^(?:no|nope|cancel|don'?t|do not|never ?mind|stop)\b", re.I)
_LIST = re.compile(r"\b(?:what (?:do|did) you (?:know|remember) about me|what have you (?:saved|remembered|stored)"
                   r"(?: about me)?|show (?:me )?my (?:saved )?memor(?:y|ies)|list (?:all )?my (?:saved )?memor(?:y|ies)|"
                   r"what(?:'s| is) in your memory|show (?:me )?(?:my )?saved (?:information|facts))\b", re.I)
_CLEAR = re.compile(r"^(?:please )?(?:(?:clear|delete|erase|wipe|remove) (?:all (?:of )?)?my (?:saved )?"
                    r"(?:memories|memory|saved information)|forget everything(?: about me)?(?: you know)?)$", re.I)
_FORGET = re.compile(r"^(?:please )?(?:forget|delete|remove|erase)\b(?:\s+(?:the|this|my))?(?:\s+(?:memory|fact|saved "
                     r"information))?(?:\s+(?:that|about|of))?\s*(?P<what>.*)$", re.I)
# "save that in long-term memory", "remember it", "remember this every time I log in": the fact is the
# previous message, not these words.
_REMEMBER_LAST = re.compile(r"^(?:please )?(?:save|store|keep|put|add|remember|note|memori[sz]e|don'?t forget)"
                            r"(?: (?:this|that|it|what i (?:just )?said))?(?: (?:in|to|into|inside) (?:your |my |the )?"
                            r"(?:long[- ]?term )?(?:memory|memories))?(?:[,.;]?\s*(?:and )?(?:remember it|keep it|save it)?"
                            r"(?:\s*(?:every ?time|always|forever|whenever|for later|for next time|when(?:ever)? i "
                            r"(?:log|sign) ?in|from now on)[\w\s]*)?)?$", re.I)
_REMEMBER_TAIL = re.compile(r"^(?P<fact>.+?)[,.;]?\s+(?:and\s+)?(?:please\s+)?(?:(?:save|store|put|keep|add)\s+"
                            r"(?:it|this|that)\s+(?:in|to|into)\s+(?:your\s+|my\s+|the\s+)?(?:long[- ]?term\s+)?"
                            r"(?:memory|memories)|(?:remember|memori[sz]e)\s+(?:it|this|that))\b.*$", re.I)
_REMEMBER = re.compile(r"^(?:please )?(?:remember|note|keep in mind|don'?t forget|save (?:this|that)|memori[sz]e)"
                       r"(?:\s+(?:that|this))?\s*:?\s*(?P<fact>.+)$", re.I)
_CORRECT = re.compile(r"^(?:update (?:my )?(?:saved information|memory|memories|info(?:rmation)?)|correction|actually|"
                      r"no,|that'?s (?:wrong|not right|incorrect),?)\s*(?:,|:|-)?\s*(?:to say\s+)?(?P<fact>.+)$", re.I)
_TITLE = re.compile(r"^(?:please )?(?:call me|address me as|refer to me as)\s+(?P<title>[\w .'-]{1,40})$", re.I)


def parse_command(text: str, pending: bool = False) -> Command:
    t = clean(text)
    if not t:
        return Command("none")
    if pending:
        if _CONFIRM.match(t):
            return Command("confirm")
        if _DECLINE.match(t):
            return Command("decline")
    if _LIST.search(t):
        return Command("list")
    if _CLEAR.match(t):
        return Command("clear_all")
    title = _TITLE.match(t)
    if title:
        return Command("set_title", _strip_value(title.group("title")))
    if _REMEMBER_LAST.match(t) and re.search(r"\b(?:this|that|it|said|memory|memories)\b", t, re.I):
        return Command("remember_last")
    tail = _REMEMBER_TAIL.match(t)   # "my owner's name is Roshan, save it in long-term memory"
    if tail and len(tail.group("fact").split()) >= 3:
        return Command("remember", tail.group("fact"))
    remember = _REMEMBER.match(t)
    if remember and not t.lower().startswith(("remember when", "remember how", "remember what", "do you remember")):
        return Command("remember", remember.group("fact"))
    correct = _CORRECT.match(t)
    if correct and re.match(r"^(?:my |i )", clean(correct.group("fact")), re.I):
        return Command("correct", correct.group("fact"))
    forget = _FORGET.match(t)
    if forget and not t.lower().startswith(("forget about it", "remove the", "delete the file", "delete my file")):
        return Command("forget", _strip_value(forget.group("what") or ""))
    return Command("none")


_CLAUSE_SPLIT = re.compile(r"(?<=[.!;])\s+|\n+|,?\s+and\s+(?=(?:i|i'm|my)\b)|,\s*(?=(?:i|i'm|my)\b)", re.I)


def split_clauses(text: str) -> list[str]:
    """'I live in Pune and I work as an engineer' -> ['I live in Pune', 'I work as an engineer']."""
    return [c for c in (clean(part) for part in _CLAUSE_SPLIT.split(text or "")) if c]


def parse_facts(text: str) -> list[ParsedFact]:
    """One or more facts from an explicit 'remember …' request."""
    clauses = split_clauses(re.sub(r"^(?:that|this)\s*:?\s+", "", clean(text), flags=re.I))
    facts = [parse_fact(c) for c in clauses]
    if len(facts) > 1 and all(f.structured for f in facts):
        return facts
    return [parse_fact(text)]


def extract_auto_facts(text: str) -> list[ParsedFact]:
    """Strong, stable first-person facts in a normal message (not questions, not hypotheticals)."""
    facts: list[ParsedFact] = []
    for sentence in split_clauses(text):
        s = clean(sentence)
        if not s or s.endswith("?") or re.match(r"^(?:what|who|where|when|why|how|is|are|do|does|can|could|would|if)\b",
                                                 s, re.I):
            continue
        if re.search(r"\b(?:if|maybe|perhaps|might|probably|pretend|imagine|suppose|not sure)\b", s, re.I):
            continue
        fact = parse_fact(s)
        if not fact.structured:
            continue
        if fact.key in _AUTO_KEYS_ALLOWED or fact.key.startswith("favourite ") or fact.category == "project":
            facts.append(fact)
    return facts
