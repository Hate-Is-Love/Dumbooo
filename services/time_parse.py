"""Understanding spoken times for reminders: "in 10 minutes", "at 5", "tomorrow morning",
"on Friday at 3:30 pm", "on 25 October at 9", "tonight", "after half an hour".

``parse_when(text, now)`` returns the moment and the text with the time words removed
(the reminder's title), or ``None`` if no time was found. Times without am/pm pick the next
time that hasn't passed yet ("at 7" at 8 am means 7 pm today). Pure functions: no clock
access of their own, so they are fully testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
                "thirty": 30, "forty": 40, "forty five": 45, "fifty": 50, "sixty": 60, "ninety": 90, "couple of": 2,
                "few": 3, "a few": 3, "a couple of": 2}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9
_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
PARTS_OF_DAY = {"morning": 9, "afternoon": 14, "evening": 18, "night": 21, "tonight": 20, "noon": 12,
                "midday": 12, "lunch": 13, "lunchtime": 13, "midnight": 0}
UNIT_SECONDS = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400,
                "week": 604800}

_NUM = r"(?:\d+(?:\.\d+)?|a couple of|couple of|a few|few|an?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|" \
       r"twelve|fifteen|twenty|thirty|forty five|forty|fifty|sixty|ninety)"
_UNIT = r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)"
_RELATIVE = re.compile(rf"\b(?:in|after|within)\s+(?P<n>{_NUM})\s+(?P<u>{_UNIT})(?:\s+and\s+a\s+half)?"
                       rf"(?:\s+(?:from now|later))?\b|\b(?:in|after)\s+(?P<half>half an hour|an hour and a half|"
                       rf"a quarter of an hour|quarter of an hour|half hour)\b", re.I)
_CLOCK = re.compile(r"\b(?:at|by|around|for)\s+(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\s*(?P<ampm>a\.?m\.?|p\.?m\.?)?"
                    r"(?:\s*o'?\s?clock)?(?:\s+in\s+the\s+(?P<part>morning|afternoon|evening|night))?\b"
                    r"|\b(?P<h2>\d{1,2})(?:[:.](?P<m2>\d{2}))?\s*(?P<ampm2>a\.?m\.?|p\.?m\.?)(?=\W|$)"
                    r"|\b(?:at\s+)?(?P<named>noon|midday|midnight)\b", re.I)
_DAY = re.compile(r"\b(?P<rel>day after tomorrow|tomorrow (?:morning|afternoon|evening|night)|"
                  r"this (?:morning|afternoon|evening)|today|tonight|tomorrow|tmrw|tmr)\b"
                  r"|\b(?:on\s+|this\s+|next\s+|coming\s+)?(?P<wd>monday|tuesday|wednesday|thursday|friday|saturday|"
                  r"sunday)(?:\s+(?P<wdpart>morning|afternoon|evening|night))?\b"
                  rf"|\b(?:on\s+)?(?:the\s+)?(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<mon1>{_MONTH})\b"
                  rf"|\b(?:on\s+)?(?P<mon2>{_MONTH})\s+(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?\b", re.I)
_PART_ALONE = re.compile(r"\b(?:in the |this |at )?(?P<part>morning|afternoon|evening|night|lunch ?time|lunch)\b", re.I)


@dataclass
class When:
    at: datetime
    title: str
    exact: bool = True        # False when only a part of day was given ("tomorrow morning" -> 9:00)


def _num(text: str) -> float:
    t = text.lower().strip()
    if t in NUMBER_WORDS:
        return NUMBER_WORDS[t]
    return float(t)


def _strip(text: str, span: tuple[int, int]) -> str:
    return text[:span[0]] + " " + text[span[1]:]


def _hour_24(hour: int, ampm: str | None, part: str | None) -> list[int]:
    """Candidate 24-h hours for a spoken hour, earliest preference first."""
    a = (ampm or "").lower().replace(".", "")
    if a == "am":
        return [0 if hour == 12 else hour]
    if a == "pm":
        return [12 if hour == 12 else hour + 12]
    if part in ("afternoon", "evening", "night"):
        return [hour + 12 if hour < 12 else hour]
    if part == "morning":
        return [0 if hour == 12 else hour]
    if hour == 0 or hour > 12:
        return [hour % 24]
    if 1 <= hour <= 6:
        return [hour + 12, hour]  # "at 3" usually means 3 pm
    return [hour % 12, hour % 12 + 12] if hour != 12 else [12, 0]


def _day_from(m: re.Match, now: datetime) -> tuple[date, str | None]:
    """The date a day expression refers to, and the part of day it implies."""
    today = now.date()
    if m.group("rel"):
        rel = m.group("rel").lower()
        if rel in ("today",):
            return today, None
        if rel == "tonight":
            return today, "tonight"
        if rel.startswith("this "):
            return today, rel.split()[1]
        if rel == "day after tomorrow":
            return today + timedelta(days=2), None
        part = rel.split()[1] if " " in rel else None
        return today + timedelta(days=1), part
    if m.group("wd"):
        target = WEEKDAYS.index(m.group("wd").lower())
        ahead = (target - today.weekday()) % 7
        if ahead == 0 or "next" in m.group(0).lower():
            ahead = ahead or 7
        return today + timedelta(days=ahead), m.group("wdpart")
    day = int(m.group("d1") or m.group("d2"))
    month = MONTHS.get((m.group("mon1") or m.group("mon2") or "").lower())
    if month is None:
        raise ValueError("not a month")
    year = today.year
    candidate = date(year, month, day)
    if candidate < today:
        candidate = date(year + 1, month, day)
    return candidate, None


def parse_when(text: str, now: datetime) -> When | None:
    """Find a reminder time in ``text``. Returns None when there is no time expression."""
    work = " " + (text or "") + " "
    # 1. Relative: "in 10 minutes", "after half an hour"
    m = _RELATIVE.search(work)
    if m:
        if m.group("half"):
            seconds = {"half an hour": 1800, "half hour": 1800, "an hour and a half": 5400,
                       "a quarter of an hour": 900, "quarter of an hour": 900}[m.group("half").lower()]
        else:
            unit = re.sub(r"s$", "", m.group("u").lower())
            unit = {"secs": "sec", "mins": "min", "hrs": "hr"}.get(unit, unit)
            seconds = _num(m.group("n")) * UNIT_SECONDS[unit]
            if "and a half" in m.group(0).lower():
                seconds += UNIT_SECONDS[unit] / 2
        return When(now + timedelta(seconds=seconds), _title(_strip(work, m.span())))

    # 2. Day + clock time
    day_m = None
    for candidate in _DAY.finditer(work):
        try:
            _day_from(candidate, now)
        except ValueError:
            continue
        day_m = candidate
        break
    clock_m = _CLOCK.search(work)
    if day_m is None and clock_m is None:
        part_m = _PART_ALONE.search(work)
        if part_m and re.search(r"\b(?:this|in the|tonight|at)\b", part_m.group(0), re.I):
            part = part_m.group("part").lower().replace(" ", "")
            at = datetime.combine(now.date(), time(PARTS_OF_DAY.get(part, 9)))
            if at <= now:
                at += timedelta(days=1)
            return When(at, _title(_strip(work, part_m.span())), exact=False)
        return None

    day, part = (_day_from(day_m, now) if day_m else (None, None))
    hour = minute = None
    ampm = None
    if clock_m:
        if clock_m.group("named"):
            hour, minute = PARTS_OF_DAY[clock_m.group("named").lower()], 0
            ampm = "named"
        else:
            hour = int(clock_m.group("h") or clock_m.group("h2"))
            minute = int(clock_m.group("m") or clock_m.group("m2") or 0)
            ampm = clock_m.group("ampm") or clock_m.group("ampm2")
            part = clock_m.group("part") or part
        if hour > 23 or minute > 59:
            return None

    spans = sorted([s.span() for s in (day_m, clock_m) if s is not None], reverse=True)
    rest = work
    for span in spans:
        rest = _strip(rest, span)
    # A part of day said separately: "at 7 in the evening" is inside the clock match; "tomorrow evening" in the day.
    pm = _PART_ALONE.search(rest)
    if pm and part is None and hour is not None and not ampm:
        part = pm.group("part").lower()
        rest = _strip(rest, pm.span())

    if hour is None:  # a day without a time: use the part of day, or 9:00
        hour = PARTS_OF_DAY.get(part or "", 9)
        at = datetime.combine(day, time(hour))
        if at <= now:
            at += timedelta(days=1)
        return When(at, _title(rest), exact=False)

    hours = [hour] if ampm == "named" else _hour_24(hour, ampm, "night" if part == "tonight" else part)
    start_day = day or now.date()
    for offset in range(0, 8):
        d = start_day + timedelta(days=offset)
        for h in hours:
            at = datetime.combine(d, time(h % 24, minute))
            if at > now:
                if day is not None and d != day:
                    break  # a named day whose time already passed: don't silently move it
                return When(at, _title(rest))
        if day is not None:
            return When(datetime.combine(day, time(hours[0] % 24, minute)) + timedelta(days=7 if day_m and
                        day_m.group("wd") else 0), _title(rest))
    return None


def _title(text: str) -> str:
    t = " ".join(text.split())
    t = re.sub(r"^(?:(?:please|hey|ok|okay)\s+)?(?:can you\s+|could you\s+)?", "", t, flags=re.I)
    t = re.sub(r"^(?:remind me|set (?:a |an )?(?:reminder|alarm)|create (?:a |an )?reminder|add (?:a |an )?reminder|"
               r"make (?:a |an )?reminder|put (?:a |an )?reminder|don'?t let me forget|wake me up)\b", "", t, flags=re.I)
    t = re.sub(r"^\s*(?:for|me|on|at)\b", "", t.strip(), flags=re.I).strip()
    t = re.sub(r"^(?:to|that|about|of|for)\s+", "", t.strip(), flags=re.I)
    t = re.sub(r"\s+(?:to|that|about|for|on|at)$", "", t.strip(), flags=re.I)
    t = re.sub(r"\b(?:to|that|about)\s+(?=(?:to|that|about)\b)", "", t, flags=re.I)
    t = re.sub(r"\s{2,}", " ", t).strip(" ,.!?-")
    t = re.sub(r"^(?:to|that|about)\s+", "", t, flags=re.I)
    t = re.sub(r"\bmy\b", "your", t, flags=re.I)
    return t[:120]


def describe(at: datetime, now: datetime) -> str:
    """'today at 5:00 PM', 'tomorrow at 9:00 AM', 'on Friday 31 Oct at 3:30 PM', 'in 10 minutes'."""
    delta = at - now
    clock = at.strftime("%I:%M %p").lstrip("0")
    if timedelta(0) <= delta < timedelta(minutes=60):
        minutes = max(1, round(delta.total_seconds() / 60))
        return f"in {minutes} minute{'s' if minutes != 1 else ''} (at {clock})"
    if at.date() == now.date():
        return f"today at {clock}"
    if at.date() == now.date() + timedelta(days=1):
        return f"tomorrow at {clock}"
    if 0 < (at.date() - now.date()).days < 7:
        return f"on {at.strftime('%A')} at {clock}"
    return f"on {at.strftime('%A')} {at.day} {at.strftime('%B')} at {clock}"
