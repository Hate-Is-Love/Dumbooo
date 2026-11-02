"""The greeting after signing in: time of day, how you signed in, and who DUMBOO is.

The first time a user signs in, DUMBOO introduces itself in a few sentences; after that the
greeting is short (plus today's reminders, if any). Users can switch it off on the ACCOUNT page.
"""

from __future__ import annotations

from datetime import datetime

INTRO = ("I'm DUMBOO, your personal desktop assistant. Talk to me by saying “Hey DUMBOO”, by clicking the core, "
         "or by typing. I can answer questions, search the web and the latest news, set reminders, check the "
         "weather, translate, remember what matters to you, and control your laptop. Just ask.")
INTRO_SPOKEN = ("I'm DUMBOO, your personal desktop assistant. Just say Hey DUMBOO, click the core, or type. "
                "I can answer questions, search the web and the news, set reminders, check the weather, translate, "
                "remember what matters to you, and control your laptop.")


def part_of_day(now: datetime) -> str:
    h = now.hour
    if 5 <= h < 12:
        return "Good morning"
    if 12 <= h < 17:
        return "Good afternoon"
    if 17 <= h < 23:
        return "Good evening"
    return "Hello"


def build_greeting(name: str, now: datetime, first_time: bool, method: str = "password",
                   reminders_today: int = 0) -> tuple[str, str]:
    """(text for the chat, sentence to speak)."""
    hello = f"{part_of_day(now)}{', ' + name if name else ''}."
    recognised = "Face recognised. " if method == "face" else ""
    if first_time:
        return f"{recognised}{hello} {INTRO}", f"{recognised}{hello} {INTRO_SPOKEN}"
    if reminders_today:
        extra = f" You have {reminders_today} reminder{'s' if reminders_today != 1 else ''} today."
    else:
        extra = ""
    line = f"{recognised}{hello} Welcome back. I'm DUMBOO, ready when you are.{extra}"
    return line, line
