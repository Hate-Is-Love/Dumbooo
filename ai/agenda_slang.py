"""Casual, slang and Hinglish phrasing -> plain task / schedule / reminder commands.

"yo ping me tmrw at 5 to hit the gym", "put milk on my list", "what's on my plate today",
"knocked out the report", "bump the dentist to friday", "kal 5 baje yaad dila dena":
``normalize`` rewrites such requests into "remind me tomorrow at 5 to go to the gym",
"add a task milk", "what do i have to do today", "mark the report as done",
"move the dentist to friday", "remind me tomorrow at 5".

Only the agenda commands (``ai.agenda_assistant``) use the result. When no agenda command
matches, the original text goes on unchanged, so normal conversation is never rewritten.
"""

from __future__ import annotations

import re

# Words and spellings (applied word by word, case-insensitive).
_WORDS = [
    (r"tmrw|tmrow|tmr|tmorrow|tomoz|2moro|2morrow|2mrw|tomorow|tommorow|tommorrow", "tomorrow"),
    (r"2day|tdy|tday", "today"), (r"2nite|2night|tonite", "tonight"),
    (r"mins", "minutes"), (r"min", "minutes"), (r"hrs", "hours"), (r"hr", "hour"), (r"secs", "seconds"),
    (r"b4", "before"), (r"w/", "with"), (r"u", "you"), (r"ur", "your"), (r"pls|plz|plez", "please"),
    (r"gonna", "going to"), (r"wanna", "want to"), (r"gotta|hafta|gotta go", "have to"), (r"lemme", "let me"),
    (r"gimme", "give me"), (r"imma|ima|i'mma", "i'm going to"), (r"needa|needta", "need to"),
    (r"whats", "what's"), (r"wats|wat's", "what's"), (r"wat", "what"), (r"hows", "how's"), (r"im", "i'm"),
    (r"dont", "don't"), (r"cant", "can't"), (r"ive", "i've"), (r"thru", "through"), (r"rn", "now"),
    (r"mtg|meetin", "meeting"), (r"appt|apt", "appointment"), (r"bday|b-day", "birthday"),
    (r"hw", "homework"), (r"assgn|assignmnt", "assignment"), (r"todo|to-do|todos|to-dos", "to-do"),
    # Hinglish / Marathi time words
    (r"kal", "tomorrow"), (r"aaj|aj", "today"), (r"parso", "day after tomorrow"), (r"subah|sakali", "morning"),
    (r"dopahar|dupahar", "afternoon"), (r"shaam|sham|sandhyakali", "evening"), (r"raat|ratri", "night"),
]
_WORD_RE = [(re.compile(rf"(?<![\w'/-])(?:{p})(?![\w'/-])", re.I), r) for p, r in _WORDS]

_LEAD = re.compile(r"^(?:(?:yo+|bro|bruh|dude|man|hey|hi|hello|ok(?:ay)?|so|um+|uh+|aye|oi|listen|alright|right|"
                   r"look|ayy+|sup|dumboo|dumbo|buddy|boss|yaar|arre|are)[\s,!.]+)+", re.I)
_BAJE = re.compile(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*baje\b", re.I)   # "5 baje" = "at 5"
_TAIL = re.compile(r"(?:[\s,]+(?:real quick|quickly|quick|asap|rn|right now|pls|plz|please|bro|bruh|dude|man|mate|for me|yaar|na|"
                   r"thanks|thank you|thx|ty|ok|okay|boss|buddy|dumboo|dumbo|will you|can you|k))+[\s.!?]*$", re.I)

_RULES: list[tuple[re.Pattern, str]] = [(re.compile(p, re.I), r) for p, r in [
    # Hinglish reminders: "yaad dila dena", "mujhe ... yaad dilana"
    (r"^(?:mujhe\s+)?(?P<x>.+?)\s+(?:ke liye\s+)?yaad (?:dila(?:na|o| dena| do| de)|karwa(?:na|o| dena))$",
     r"remind me \g<x>"),
    (r"^yaad (?:dila(?:na|o| dena| do| de)|karwa(?:na|o| dena))\s+(?P<x>.+)$", r"remind me \g<x>"),
    # "ping me / buzz me / hit me up / give me a heads up / lemme know / let me know" -> remind me
    (r"^(?:can you |could you |pls |please )?(?:ping|buzz|nudge|poke|bug|text|alert|holler at|shout at|hit) me(?: up)?"
     r"(?: with a (?:reminder|heads up))?\b", "remind me"),
    (r"^(?:give|send) me a (?:heads[- ]up|shout|nudge|ping|reminder)\b", "remind me"),
    (r"^let me know\b(?=.*\b(?:to|at|in|when|tomorrow|today|tonight)\b)", "remind me"),
    (r"^(?:don'?t let me forget|make sure i (?:don'?t forget|remember)|make sure i)\b(?: to)?", "remind me to"),
    (r"^(?:i (?:can'?t|cannot|must not|mustn'?t) forget|i better not forget|note to self:?)(?: to)?\b",
     "remind me to"),
    # tasks: "put X on my list", "jot down X", "add X to the list", "throw X on my to-do"
    (r"^(?:put|stick|throw|slap|chuck|pop|jot|write|note|add)(?: down)? (?P<x>.+?) (?:on|in|to|onto|into) (?:my |the )?"
     r"(?:list|to-do(?: list)?|tasks?|task list|plate)$", r"add a task \g<x>"),
    (r"^(?:jot down|note down|write down) (?:a task:? )?(?P<x>.+)$", r"add a task \g<x>"),
    (r"^new (?:task|to-do):? (?P<x>.+)$", r"add a task \g<x>"),
    # agenda: "what's on my plate", "what's the plan", "what's my day looking like"
    (r"^(?:what'?s|what is|whats) (?:on my plate|the (?:game )?plan|poppin(?:g)?|cooking|on deck|the move|"
     r"the agenda|my agenda|happening|lined up|up next)(?P<w> (?:for )?(?:today|tomorrow|tonight|this week))?\??$",
     r"what do i have to do\g<w>"),
    (r"^(?:what'?s|what is|how'?s|how is) my (?:day|week|tomorrow) (?:looking|look|going to look)(?: like)?\??$",
     "what do i have to do"),
    (r"^(?:what|wat) (?:am i doing|do i (?:got|have) (?:going on|on|lined up|planned))(?P<w> (?:for )?(?:today|"
     r"tomorrow|tonight|this week))?\??$", r"what do i have to do\g<w>"),
    (r"^(?:what'?s up|wassup|sup) (?P<w>today|tomorrow|this week)\??$", r"what do i have to do \g<w>"),
    (r"^(?:anything|stuff) (?:i (?:have|need) to do|to do|due|pending)(?P<w> (?:today|tomorrow|this week))?\??$",
     r"what do i have to do\g<w>"),
    (r"^(?:aaj|today) (?:kya|kya kya) (?:karna|plan) hai\??$", "what do i have to do today"),
    (r"^(?P<w>today|tomorrow|day after tomorrow) (?:ka|ke|ki|cha|che) (?:plan|schedule|kaam|program)(?: kya hai| kay "
     r"aahe)?\??$", r"what do i have to do \g<w>"),
    (r"^(?:kal|tomorrow) (?:kya|kya kya) (?:karna|plan) hai\??$", "what do i have to do tomorrow"),
    # done: "knocked out X", "i'm done with X", "X is done", "smashed X"
    (r"^(?:i(?:'m| am)? )?(?:done with|finished with|through with) (?P<x>.+)$", r"mark \g<x> as done"),
    (r"^(?:i )?(?:knocked out|smashed|nailed|crushed|wrapped up|got done|finished up|crossed off|ticked off|checked off|"
     r"completed|finished) (?P<x>.+)$", r"mark \g<x> as done"),
    (r"^(?:cross|tick|check|strike) (?P<x>.+?) off(?: (?:my|the) (?:list|to-do(?: list)?))?$", r"mark \g<x> as done"),
    (r"^(?P<x>.+?) (?:is|are) (?:done|finished|completed|sorted|handled|taken care of)$", r"mark \g<x> as done"),
    (r"^(?P<x>.+?) (?:ho gaya|ho gya|hogaya|kar liya|done hai)$", r"mark \g<x> as done"),
    # cancel / move / book
    (r"^(?:scrap|ditch|nix|bin|axe|kill|scratch|bail on|call off|skip) (?P<x>.+)$", r"cancel \g<x>"),
    (r"^(?:bump|push|slide|shove|kick) (?P<x>.+?) (?:back |forward )?(?:to|till|until|for) (?P<t>.+)$",
     r"move \g<x> to \g<t>"),
    (r"^(?:pencil in|line up|set up|lock in|book in|fix) (?P<x>.+)$", r"schedule \g<x>"),
    # free / busy
    (r"^(?:am i|r u|are you|is my calendar) (?:free|clear|open|available|busy)(?P<t>.*)$", r"am i free\g<t>"),
    (r"^(?:do i (?:got|have) (?:anything|something|stuff)|anything on)(?P<t> .+)?\??$", r"am i free\g<t>"),
]]


def normalize(text: str) -> str:
    """Plain wording for agenda commands. Returns lower-noise text; never raises."""
    t = " ".join((text or "").strip().split())
    if not t:
        return t
    t = re.sub(r"[.!]+$", "", t).strip()
    for _ in range(2):  # "yo bro, ..." -> "..."
        t = _LEAD.sub("", t)
    t = _TAIL.sub("", t).strip(" ,")
    for pattern, repl in _WORD_RE:
        t = pattern.sub(repl, t)
    t = _BAJE.sub(lambda m: f"at {m.group(1)}" + (f":{m.group(2)}" if m.group(2) else ""), t)
    for pattern, repl in _RULES:
        new = pattern.sub(repl, t, count=1)
        if new != t:
            t = new
            break
    return " ".join(t.split())
