"""Tasks, schedule and "what do I have to do" by voice or text, including slang.

* **Tasks** (TASKS page): "add a task to pay rent by Friday", "put milk on my list",
  "what are my tasks", "anything overdue", "mark the report as done" / "knocked out the report",
  "delete the task about milk", "move pay rent to Monday", "make pay rent high priority",
  "clear completed tasks". A task with a deadline gets its own reminder (spoken when due).
* **Schedule** (SCHEDULE page): "schedule a meeting with Rahul tomorrow at 3", "I have a dentist
  appointment on Friday at 4, remind me an hour before", "what's on my schedule tomorrow",
  "move the dentist to 6 pm", "cancel the meeting with Rahul", "am I free at 3 tomorrow",
  "when is my dentist appointment".
* **Agenda**: "what do I have to do today" / "what's on my plate" / "what's next": events,
  reminders and tasks together, each with its time.
* **Reminders go where they belong**: "remind me to submit the report by Friday" is a to-do with a
  deadline, so it is saved in TASKS with a reminder; "remind me to call mom at 5" stays a reminder
  on the SCHEDULE page (``EverydayAssistant``); "remind me to pay rent at 9" when a task "Pay rent"
  exists sets that task's reminder instead of adding a duplicate.
* After a task reminder: "done" completes it, "snooze" moves its reminder. "undo" reverses the last
  change made here.

Slang and Hinglish are rewritten first (``ai.agenda_slang``). Phrasings the rules don't cover but
that clearly mention tasks, schedule or reminders are read by the AI (JSON, validated) when it is
available. Anything else goes on unchanged to the next hook.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Callable

from ai.agenda_slang import normalize
from ai.controller import ConversationHooks, LocalReply
from services.auth_service import AuthError, Session
from services.task_service import Task, ValidationError
from services.time_parse import _CLOCK, _DAY, _RELATIVE, describe, parse_when

log = logging.getLogger(__name__)

PENDING_TTL_S = 90
FOLLOWUP_S = 30 * 60          # "done" / "snooze" refer to a task reminder for this long
TASK_REMIND_BEFORE = timedelta(minutes=30)   # a timed deadline is announced this long before
DEFAULT_EVENT_REMINDER = 10
EVENT_HOURS = 1               # events have no end time: "am I free" assumes one hour

EVENT_WORDS = (r"meeting|meet|appointment|appt|event|call with|video call|zoom|class|lecture|lab|exam|test|quiz|"
               r"interview|party|dinner|lunch|breakfast|brunch|session|visit|flight|train|bus|doctor|dentist|"
               r"checkup|check-up|gym|workout|match|game|birthday|anniversary|presentation|webinar|seminar|"
               r"workshop|date|concert|movie|show|trip|tuition|coaching|practice|rehearsal|ceremony|wedding|"
               r"standup|stand-up|sync|demo|deadline")
_EVENT_RE = re.compile(rf"\b(?:{EVENT_WORDS})s?\b", re.I)
_STOP = {"the", "a", "an", "my", "your", "to", "for", "with", "about", "of", "on", "at", "in", "task", "tasks",
         "to-do", "todo", "item", "event", "reminder", "that", "this", "it", "please", "called", "named", "list"}

# -- command patterns (on the normalised text) ------------------------------------------------------------
_RANGE = r"(?P<when>today|tonight|tomorrow|this week|next week|this weekend|(?:on )?(?:monday|tuesday|wednesday|" \
         r"thursday|friday|saturday|sunday))"
RE_ADD_TASK = [re.compile(p, re.I) for p in (
    r"^(?:add|create|make|put|set up|save|start)(?: a| an| the| one| another)?(?: new)? (?:task|to-do)(?: item)?"
    r"(?: (?:called|named|to|for|about))?[:,]?\s+(?P<x>.+)$",
    r"^add (?P<x>.+?) to (?:my |the )?(?:tasks?|task list|to-do(?: list)?|list|plate)$",
    r"^(?:i (?:have|need|got) to|i've got to|i must|i should|i gotta) (?P<x>.+?\b(?:by|before|due|until|till)\b.+)$",
    r"^(?:new )?(?:task|to-do)[:,]\s*(?P<x>.+)$")]
RE_LIST_TASKS = re.compile(
    r"^(?:what are|what're|show|list|read(?: out)?|tell me|check|open|give me|get|see)(?: me)?(?: all)?(?: of)?"
    r" (?:my |the )?(?:pending |open |remaining |current |outstanding |unfinished |overdue |completed |done )?"
    r"(?:tasks?|to-dos?|to-do list|task list)(?: (?:for |due )?" + _RANGE + r")?\??$|"
    r"^(?:what(?:'s| is) (?:pending|left|overdue|remaining|due)(?: (?:on my list|for me))?(?: " + _RANGE.replace(
        "?P<when>", "?P<when2>") + r")?|do i have any (?:pending |overdue |open )?(?:tasks|to-dos)(?: " +
    _RANGE.replace("?P<when>", "?P<when3>") + r")?|any (?:overdue|pending|open) (?:tasks|to-dos)|"
    r"(?:is there |do i have )?anything (?:overdue|pending|left(?: to do)?|outstanding)|"
    r"what(?:'s| is) (?:on|in) (?:my |the )?(?:to-do list|to-dos|task list|tasks)|"
    r"(?:my )?(?:tasks|to-do list|to-dos)|what (?:tasks|to-dos) do i have(?: " +
    _RANGE.replace("?P<when>", "?P<when4>") + r")?)\??$", re.I)
RE_DONE = [re.compile(p, re.I) for p in (
    r"^(?:mark|set|tick|check|cross)(?: off)?(?: the)?(?: task)? (?P<x>.+?)(?: (?:as )?(?:done|complete|completed|"
    r"finished))$",
    r"^(?:mark|tick off|check off|cross off)(?: the)? task (?P<x>.+)$",
    r"^(?:complete|finish|close)(?: the)? task (?P<x>.+)$")]
RE_DONE_LAST = re.compile(r"^(?:done|did it|i did it|finished|completed|it'?s done|mark (?:it|that) (?:as )?(?:done|"
                          r"complete)|(?:i'?m )?done with (?:it|that)|ho gaya|kar liya)\??$", re.I)
RE_DELETE_TASK = re.compile(r"^(?:delete|remove|cancel|drop|clear|erase|scrap)(?: the| my)? (?:task|to-do)(?: item)?"
                            r"(?: (?:called|named|about|for|to))? (?P<x>.+)$|^(?:remove|delete|take|drop) (?P<x2>.+?) "
                            r"(?:off|from) (?:my |the )?(?:list|tasks?|task list|to-do(?: list)?)$", re.I)
RE_CLEAR_DONE = re.compile(r"^(?:clear|delete|remove)(?: all)?(?: (?:my|the))? (?:completed|finished|done) "
                           r"(?:tasks|to-dos)$", re.I)
RE_PRIORITY = re.compile(r"^(?:make|set|mark|change)(?: the)?(?: task)? (?P<x>.+?)(?: (?:to|as))? (?:a )?(?P<p>high|"
                         r"top|urgent|important|medium|normal|low)(?: priority)?$|^(?:set|change)(?: the)? priority "
                         r"of (?P<x2>.+?) to (?P<p2>high|medium|low)$", re.I)
RE_ADD_EVENT = [re.compile(p, re.I) for p in (
    r"^(?:schedule|book)(?: a| an| my| the)? (?P<x>.+)$",   # "plan a trip …" is a question for the AI
    r"^(?:add|create|put|set|make|enter|save)(?: a| an| my| the)?(?: new)? (?P<x>.+?)(?: (?:to|on|in|into) (?:my |the )?"
    r"(?:schedule|calendar|agenda|diary))?$",
    r"^(?:i(?:'ve| have)?|i've got|i got|there(?:'s| is)) (?:a |an |my |the )?(?P<x>.+)$")]
RE_LIST_SCHEDULE = re.compile(
    r"^(?:what(?:'s| is)|what do i have|show|list|read|tell me|check|open|give me|see)(?: me)? (?:on )?(?:my |the )?"
    r"(?:schedule|calendar|agenda|events|meetings|appointments|plans)(?: (?:for|on))?(?: " + _RANGE + r")?\??$|"
    r"^(?:do i have|have i got|are there)(?: any)? (?:meetings|events|appointments|classes|plans)(?: (?:for |on )?" +
    _RANGE.replace("?P<when>", "?P<when2>") + r")?\??$|"
    r"^what (?:meetings|events|appointments|classes) do i have(?: " + _RANGE.replace("?P<when>", "?P<when3>") +
    r")?\??$|^(?:what(?:'s| is) on|what(?:'s| is) happening)(?: " + _RANGE.replace("?P<when>", "?P<when4>") + r")\??$",
    re.I)
RE_AGENDA = re.compile(
    r"^(?:what (?:do|should|must) i (?:have to |need to |got to )?do|what do i need to do|what(?:'s| is) my plan|"
    r"what(?:'s| is) (?:planned|on for)|what are my plans|what have i got|what(?:'s| is) on my (?:list|plate)|"
    r"(?:give me|tell me) (?:my|the) (?:plan|agenda|rundown)|(?:my )?(?:daily )?(?:briefing|agenda|rundown)|"
    r"plan for)(?: (?:for )?" + _RANGE + r")?\??$|^what(?:'s| is) (?:next|coming up|up next)(?: for me)?\??$|"
    r"^what(?:'s| is) my next (?:task|event|meeting|reminder|thing)\??$", re.I)
RE_NEXT = re.compile(r"^what(?:'s| is) (?:next|coming up|up next)|^what(?:'s| is) my next", re.I)
RE_FREE = re.compile(r"^am i free(?P<t>.*)$", re.I)
RE_WHEN_IS = re.compile(r"^when(?:'s| is| was| do i have| am i| is my| do i need to do| should i do)(?: my| the)? "
                        r"(?P<x>.+?)(?: due| scheduled| happening| planned)?\??$", re.I)
RE_CANCEL = re.compile(r"^(?:cancel|delete|remove|call off|drop|erase|clear)(?: the| my| that| this| our)? (?P<x>.+)$",
                       re.I)
RE_MOVE = re.compile(r"^(?:move|reschedule|re-schedule|shift|push|postpone|change|prepone|bring forward|delay)"
                     r"(?: the| my| our)? (?P<x>.+?) (?:to|till|until|for) (?P<t>.+)$", re.I)
RE_REMIND = re.compile(r"^remind me\b(?P<rest>.*)$", re.I)
RE_SNOOZE = re.compile(r"^(?:snooze(?: (?:it|that|the reminder))?(?: (?:for|by) (?P<n>\d+) (?:minutes?|mins?))?|"
                       r"remind me (?:again|later)(?: in (?P<n2>\d+) (?:minutes?|mins?))?)$", re.I)
RE_UNDO = re.compile(r"^(?:undo|undo (?:that|it|the last (?:change|one))|revert(?: that)?|take that back|"
                     r"put it back|bring it back)$", re.I)
RE_YES = re.compile(r"^(?:yes|yeah|yep|yup|sure|ok|okay|do it|go ahead|confirm|haan|ha)\b", re.I)
RE_NO = re.compile(r"^(?:no|nope|nah|cancel|never ?mind|forget it|stop|don'?t|nahi)\b", re.I)
_CUE = re.compile(r"\b(?:tasks?|to-dos?|schedule|calendar|agenda|meetings?|appointments?|deadline|due|remind|"
                  r"reminders?|plans?|planned|events?)\b", re.I)
_SELF = re.compile(r"\b(?:my|me|i|i'm|i've|add|put|set|move|cancel|delete|mark|schedule|book)\b", re.I)


@dataclass
class _Pending:
    kind: str           # "event_time" | "clear_done"
    data: dict
    expires: float


def _clean(text: str) -> str:
    t = re.sub(r"^\s*(?:(?:hey|hi|ok|okay|yo)\s+)?(?:dumboo|dumbo)?\s*[,:]?\s*(?:please\s+|can you\s+|could you\s+|"
               r"would you\s+|will you\s+)?", "", text or "", flags=re.I).strip()
    return re.sub(r"[\s.!]+$", "", t).strip()


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _words(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9'-]+", (text or "").lower()) if w not in _STOP}


def _cap(title: str) -> str:
    title = " ".join(title.split()).strip(" ,.-")
    return title[:1].upper() + title[1:]


def _clock(at: datetime | time) -> str:
    return at.strftime("%I:%M %p").lstrip("0")


def _task_title(text: str) -> str:
    t = " ".join(text.split())
    t = re.sub(r"^(?:to|that i|that|i need to|i have to|i must|i gotta|need to|have to)\s+", "", t, flags=re.I)
    t = re.sub(r"\s+(?:by|before|due|until|till|on|at|for|and|,)$", "", t, flags=re.I)
    t = re.sub(r"\byour\b", "my", t, flags=re.I)  # titles read from your point of view: "Finish my homework"
    return _cap(t)[:200]


def _event_title(text: str) -> str:
    t = re.sub(r"\s+(?:to|on|in|into) (?:my |the )?(?:schedule|calendar|agenda|diary)$", "", " ".join(text.split()),
               flags=re.I)
    t = re.sub(r"^(?:a|an|the|my|our)\s+", "", t, flags=re.I)
    t = re.sub(r"\s+(?:on|at|for|from)$", "", t, flags=re.I)
    t = re.sub(r"\byour\b", "my", t, flags=re.I)
    return _cap(t)[:200]


class AgendaAssistant(ConversationHooks):
    def __init__(self, inner: ConversationHooks, auth, tasks=None, schedule=None,
                 generate: Callable[..., str] | None = None, clock: Callable[[], datetime] = datetime.now,
                 monotonic: Callable[[], float] = _time.monotonic) -> None:
        self._inner = inner
        self._auth = auth
        self._tasks = tasks
        self._schedule = schedule
        self._generate = generate
        self._clock = clock
        self._mono = monotonic
        self._pending: dict[str, _Pending] = {}
        self._fired: dict[int, tuple[str, int, str, float]] = {}   # user -> (kind, id, title, when) last reminder
        self._undo: dict[int, tuple[str, Callable[[], Any]]] = {}  # user -> (description, how to undo)
        self._lock = threading.Lock()

    # -- chain -----------------------------------------------------------------------------------------------
    def prompt_options(self, scope: Any) -> dict:
        return self._inner.prompt_options(scope)

    def extra_context(self, scope: Any, text: str, seq: int) -> str:
        return self._inner.extra_context(scope, text, seq)

    def forget_session(self, scope: Session | None) -> None:
        if scope is not None:
            with self._lock:
                self._pending.pop(scope.token_hash, None)
        if hasattr(self._inner, "forget_session"):
            self._inner.forget_session(scope)

    def __getattr__(self, name: str):  # attributes of the hooks further down the chain
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def reminder_fired(self, user_id: int, kind: str, item_id: int, title: str) -> None:
        """A reminder was delivered: "done" and "snooze" now refer to it."""
        with self._lock:
            self._fired[user_id] = (kind, item_id, title, self._mono())
        if kind == "event" and hasattr(self._inner, "reminder_fired"):
            self._inner.reminder_fired(user_id, item_id, title)

    # -- dispatch ---------------------------------------------------------------------------------------------
    def local_reply(self, scope: Any, text: str, source: str) -> LocalReply | None:
        if not isinstance(scope, Session) or self._tasks is None or self._schedule is None:
            return self._inner.local_reply(scope, text, source)
        original = _clean(text)
        plain = normalize(original)
        try:
            reply, passed = self._handle(scope, original, plain)
        except AuthError as exc:
            return LocalReply(exc.user_message, kind="auth")
        except ValidationError as exc:
            return LocalReply(f"I couldn't save that: {exc}.", kind="task")
        except Exception:
            log.exception("Agenda request failed")
            reply, passed = None, None
        if reply is not None:
            return reply
        inner = self._inner.local_reply(scope, passed or text, source)
        if inner is not None or not self._ai_candidate(plain):
            return inner
        # Nothing else understood it, but it is clearly about tasks / schedule / reminders: let the AI read it.
        try:
            user_id = self._user_or_none(scope)
            return self._ai_command(scope, user_id, original) if user_id is not None else None
        except (AuthError, ValidationError):
            return None
        except Exception:
            log.exception("Agenda AI request failed")
            return None

    def _ai_candidate(self, t: str) -> bool:
        return bool(self._generate is not None and t and len(t) < 300 and _CUE.search(t) and _SELF.search(t)
                    and not re.match(r"^(?:how|why|explain|what is|what are|what does|who|tell me about|can you "
                                     r"explain|is it|should i use|which)\b", t, re.I))

    def _user(self, scope: Session) -> int:
        return self._auth.require(scope, "tasks.manage").id

    def _set_pending(self, scope: Session, kind: str, **data) -> None:
        with self._lock:
            self._pending[scope.token_hash] = _Pending(kind, data, self._mono() + PENDING_TTL_S)

    def _take_pending(self, scope: Session) -> _Pending | None:
        with self._lock:
            p = self._pending.pop(scope.token_hash, None)
        return p if p and p.expires > self._mono() else None

    def _recent_task_reminder(self, user_id: int) -> tuple[int, str] | None:
        with self._lock:
            fired = self._fired.get(user_id)
        if fired and fired[0] == "task" and self._mono() - fired[3] < FOLLOWUP_S:
            return fired[1], fired[2]
        return None

    def _handle(self, scope: Session, original: str, t: str) -> tuple[LocalReply | None, str | None]:
        """(reply, text for the next hook). The next hook gets the plain text only for reminders."""
        pending = self._take_pending(scope)
        if pending is not None:
            reply = self._resume(scope, pending, t)
            if reply is not None:
                return reply, None
        if not t:
            return None, None
        user_id = self._user_or_none(scope)
        if user_id is None:
            return None, None
        if RE_UNDO.match(t):
            return self._undo_last(user_id), None
        recent = self._recent_task_reminder(user_id)
        if recent and RE_DONE_LAST.match(t):
            return self._complete(scope, user_id, [self._tasks.get(scope, recent[0])]), None
        m = RE_SNOOZE.match(t)
        if m and recent:
            minutes = int(m.group("n") or m.group("n2") or 10)
            task = self._tasks.snooze(scope, recent[0], minutes)
            return LocalReply(f"😴 Okay — I'll remind you about **{task.title}** again "
                              f"{describe(task.remind_at, self._clock())}.",
                              speech=f"Okay, I'll remind you again in {minutes} minutes.", kind="task"), None

        if RE_CLEAR_DONE.match(t):
            done = self._tasks.list(scope, True)
            if not done:
                return LocalReply("There are no completed tasks to clear.", kind="task"), None
            self._set_pending(scope, "clear_done", count=len(done))
            return LocalReply(f"Delete all {len(done)} completed tasks? Say “yes” or “no”.", kind="task",
                              listen_after=True), None
        m = RE_LIST_TASKS.match(t)
        if m:
            when = next((m.group(g) for g in ("when", "when2", "when3", "when4") if m.group(g)), None)
            return self._list_tasks(scope, when, overdue="overdue" in t.lower(),
                                    completed=bool(re.search(r"\b(?:completed|done) (?:tasks|to-dos)", t, re.I))), None
        m = RE_AGENDA.match(t)
        if m:
            if RE_NEXT.match(t):
                return self._next(scope), None
            return self._agenda(scope, m.group("when")), None
        m = RE_LIST_SCHEDULE.match(t)
        if m:
            when = next((m.group(g) for g in ("when", "when2", "when3", "when4") if m.group(g)), None)
            return self._list_schedule(scope, when), None
        m = RE_FREE.match(t)
        if m:
            return self._free(scope, m.group("t").strip(" ?")), None
        m = RE_PRIORITY.match(t)
        if m:
            reply = self._priority(scope, user_id, m.group("x") or m.group("x2"), m.group("p") or m.group("p2"))
            if reply is not None:
                return reply, None
        for pattern in RE_DONE:
            m = pattern.match(t)
            if m:
                explicit = bool(re.search(r"\b(?:mark|task|complete|tick|cross|check)\b", original, re.I))
                reply = self._done(scope, user_id, m.group("x"), explicit)
                if reply is not None:
                    return reply, None
        m = RE_DELETE_TASK.match(t)
        if m:
            return self._delete_task(scope, user_id, m.group("x") or m.group("x2")), None
        for pattern in RE_ADD_TASK:
            m = pattern.match(t)
            if m:
                return self._add_task(scope, user_id, m.group("x")), None
        m = RE_REMIND.match(t)
        if m:
            reply = self._remind(scope, user_id, t)
            return (reply, None) if reply is not None else (None, t)   # a plain reminder: EverydayAssistant
        m = RE_MOVE.match(t)
        if m:
            reply = self._move(scope, user_id, m.group("x"), m.group("t"))
            if reply is not None:
                return reply, None
        m = RE_CANCEL.match(t)
        if m and not re.search(r"\b(?:reminders?|alarms?|timers?)\b", t, re.I):
            reply = self._cancel(scope, user_id, m.group("x"))
            if reply is not None:
                return reply, None
        m = RE_WHEN_IS.match(t)
        if m:
            reply = self._when_is(scope, m.group("x"))
            if reply is not None:
                return reply, None
        for pattern in RE_ADD_EVENT:
            m = pattern.match(t)
            if m and _EVENT_RE.search(m.group("x")) and not re.match(r"^(?:a |an )?(?:reminder|alarm|timer)\b",
                                                                     m.group("x"), re.I):
                # Without a time only a clear request adds an event ("make a presentation on X" is not one).
                explicit = bool(re.match(r"^(?:schedule|book)\b", t, re.I) or re.search(
                    r"\b(?:to|on|in|into) (?:my |the )?(?:schedule|calendar|agenda|diary)\b", t, re.I))
                reply = self._add_event(scope, m.group("x"), explicit=explicit)
                if reply is not None:
                    return reply, None
        return None, None

    def _user_or_none(self, scope: Session) -> int | None:
        try:
            return self._user(scope)
        except AuthError:
            return None

    def _resume(self, scope: Session, pending: _Pending, t: str) -> LocalReply | None:
        if pending.kind == "clear_done":
            if RE_YES.match(t):
                done = self._tasks.list(scope, True)
                for task in done:
                    self._tasks.delete(scope, task.id)
                return LocalReply(f"Done — {len(done)} completed task{'s' if len(done) != 1 else ''} deleted.",
                                  kind="task")
            return LocalReply("Okay, I kept them.", kind="task") if RE_NO.match(t) else None
        if RE_NO.match(t):
            return LocalReply("Okay, never mind.", kind="schedule")
        if pending.kind == "event_time":
            when = parse_when(t if re.match(r"^(?:in|at|on|after|tomorrow|today|tonight|next|this)\b", t, re.I)
                              else "at " + t, self._clock())
            if when is None:
                when = parse_when(t, self._clock())
            if when is None:
                return None
            return self._create_event(scope, pending.data["title"], when.at, pending.data["reminder"])
        return None

    # -- tasks ----------------------------------------------------------------------------------------------------
    def _pending_tasks(self, scope: Session) -> list[Task]:
        return self._tasks.list(scope, False)

    def _match(self, items: list, spec: str, title=lambda x: x.title) -> list:
        want = _words(spec)
        if not want:
            return []
        exact = [i for i in items if _words(title(i)) == want]
        if exact:
            return exact
        subset = [i for i in items if want <= _words(title(i))]
        if subset:
            return subset
        scored = [(len(want & _words(title(i))) / len(want), i) for i in items]
        best = max((s for s, _ in scored), default=0)
        return [i for s, i in scored if s == best and s >= 0.6]

    def _due_text(self, task: Task, now: datetime) -> str:
        if task.due_date is None:
            return ""
        if task.due_time is not None:
            return "due " + describe(task.due_at, now)
        days = (task.due_date - now.date()).days
        name = {0: "today", 1: "tomorrow", -1: "yesterday"}.get(days)
        if name is None:
            name = (f"on {task.due_date.strftime('%A')}" if 0 < days < 7 else
                    f"on {task.due_date.strftime('%a')} {task.due_date.day} {task.due_date.strftime('%b')}")
        return f"due {name}"

    def _task_line(self, task: Task, now: datetime) -> str:
        bits = [self._due_text(task, now)]
        if task.is_overdue(now):
            bits.append("**OVERDUE**")
        if task.remind_at and not task.reminded_at and task.remind_at > now:
            bits.append(f"reminder {describe(task.remind_at, now)}")
        if task.priority == "high":
            bits.append("high priority")
        extra = " — " + ", ".join(b for b in bits if b) if any(bits) else ""
        return f"- **{task.title}**{extra}"

    def _parse_task(self, x: str) -> dict:
        """Title, priority, due date/time and reminder time from an "add a task …" phrase."""
        now = self._clock()
        text = " " + x.strip() + " "
        priority = "medium"
        if re.search(r"\b(?:urgent|important|high priority|top priority|asap|high-priority)\b", text, re.I):
            priority = "high"
        elif re.search(r"\b(?:low priority|not urgent|low-priority|whenever)\b", text, re.I):
            priority = "low"
        text = re.sub(r",?\s*(?:it'?s |as |with |make it |set it to |mark it )?(?:a )?(?:urgent|important|high priority|"
                      r"top priority|asap|high-priority|low priority|not urgent|low-priority|whenever)\b(?: one)?",
                      " ", text, flags=re.I)
        remind_at = None
        no_reminder = bool(re.search(r"\b(?:no reminder|without (?:a )?reminder|don'?t remind me)\b", text, re.I))
        text = re.sub(r",?\s*(?:and )?(?:no reminder|without (?:a )?reminder|don'?t remind me)\b", " ", text, flags=re.I)
        before = None
        m = re.search(r",?\s*(?:and )?remind me (?P<r>.+)$", text, re.I)
        if m:
            rtext = m.group("r").strip()
            text = text[:m.start()] + " "
            bm = re.match(r"^(?P<n>\d+|an?|one|two|half an?|thirty|fifteen|ten|five) ?(?P<u>minutes?|mins?|hours?|"
                          r"hrs?|days?) (?:before|earlier|ahead)$", rtext, re.I)
            if bm:
                n = {"a": 1, "an": 1, "one": 1, "two": 2, "half a": 0.5, "half an": 0.5, "thirty": 30, "fifteen": 15,
                     "ten": 10, "five": 5}.get(bm.group("n").lower())
                n = n if n is not None else int(bm.group("n"))
                unit = bm.group("u").lower()
                before = timedelta(minutes=n) if unit.startswith("min") else (
                    timedelta(hours=n) if unit.startswith("h") else timedelta(days=n))
            else:
                when = parse_when(rtext if re.match(r"^(?:in|at|on|tomorrow|today|tonight|next|this)\b", rtext, re.I)
                                  else "at " + rtext, now)
                remind_at = when.at if when else None
        when = parse_when(text, now)
        due_date = due_time = None
        title_src = text
        if when is not None and not _RELATIVE.search(text):
            due_date = when.at.date()
            due_time = when.at.time() if when.exact else None
            title_src = when.title
        elif when is not None:   # "in 2 hours": a deadline from now
            due_date, due_time, title_src = when.at.date(), when.at.time(), when.title
        title = _task_title(re.sub(r"\byour\b", "my", title_src, flags=re.I))
        if not title:
            raise ValidationError("the task needs a name")
        if remind_at is None and due_date is not None and not no_reminder:
            due_at = datetime.combine(due_date, due_time or time(9, 0))
            if before is not None:
                remind_at = due_at - before
            elif due_time is not None:
                remind_at = due_at - TASK_REMIND_BEFORE if due_at - TASK_REMIND_BEFORE > now else due_at
            else:
                remind_at = due_at if due_at > now else None   # a date-only deadline: that morning at 9
        if remind_at is not None and remind_at <= now:
            remind_at = None
        return {"title": title, "priority": priority, "due_date": due_date, "due_time": due_time,
                "remind_at": remind_at}

    def _add_task(self, scope: Session, user_id: int, x: str, parsed: dict | None = None) -> LocalReply:
        data = parsed or self._parse_task(x)
        now = self._clock()
        same = [t for t in self._pending_tasks(scope) if t.title.lower() == data["title"].lower()]
        if same and data["due_date"] is None:
            return LocalReply(f"**{same[0].title}** is already on your task list.", kind="task")
        task = self._tasks.create(scope, data["title"], priority=data["priority"], due_date=data["due_date"],
                                  due_time=data["due_time"], remind_at=data["remind_at"])
        self._remember_undo(user_id, f"adding “{task.title}”", lambda: self._tasks.delete(scope, task.id))
        due = self._due_text(task, now)
        remind = describe(task.remind_at, now) if task.remind_at else ""
        text = f"✅ Added to **TASKS**: **{task.title}**" + (f" — {due}" if due else "") + \
               (" · high priority" if task.priority == "high" else "") + "."
        if remind:
            text += f"\n\n⏰ I'll remind you {remind}."
        speech = f"Added {task.title} to your tasks" + (f", {due}" if due else "") + "."
        if remind:
            speech += f" I'll remind you {remind}."
        return LocalReply(text + "\n\n_Say “what are my tasks” to hear the list, or “mark it as done” later._",
                          speech=speech, kind="task")

    def _list_tasks(self, scope: Session, when: str | None, overdue: bool = False,
                    completed: bool = False) -> LocalReply:
        now = self._clock()
        if completed:
            done = self._tasks.list(scope, True)[:12]
            if not done:
                return LocalReply("You haven't completed any tasks yet.", kind="task")
            return LocalReply("**Completed tasks:**\n\n" + "\n".join(f"- ~~{t.title}~~" for t in done),
                              speech=f"You've completed {len(done)} tasks, most recently {done[0].title}.",
                              kind="task")
        tasks = self._pending_tasks(scope)
        label, kind = "", ""     # "tasks for tomorrow" / "overdue tasks"
        if overdue:
            tasks, kind = [t for t in tasks if t.is_overdue(now)], " overdue"
        elif when:
            start, end, span = self._span(when)
            tasks = [t for t in tasks if t.due_date and start.date() <= t.due_date < end.date()
                     or (t.is_overdue(now) and start.date() <= now.date() < end.date())]
            label = f" for {span}" if not span.startswith("on ") else f" {span}"
        if not tasks:
            empty = (f"You have no{kind} tasks{label}." if label or kind else
                     "Your task list is empty. Say “add a task to …” to add one.")
            return LocalReply(empty, kind="task")
        lines = [self._task_line(t, now) for t in tasks[:15]]
        more = f"\n\n…and {len(tasks) - 15} more on the TASKS page." if len(tasks) > 15 else ""
        late = [t for t in tasks if t.is_overdue(now)]
        spoken = []
        for t in tasks[:4]:
            due = self._due_text(t, now)
            spoken.append(t.title + (f", {due}" if due else ""))
        speech = f"You have {len(tasks)}{kind} task{'s' if len(tasks) != 1 else ''}{label}"
        if late and not overdue:
            speech += f", {len(late)} overdue"
        speech += ": " + "; ".join(spoken) + "."
        return LocalReply(f"**Your{kind} tasks{label}** ({len(tasks)}):\n\n" + "\n".join(lines) + more, speech=speech,
                          kind="task")

    def _complete(self, scope: Session, user_id: int, chosen: list[Task]) -> LocalReply:
        task = chosen[0]
        self._tasks.set_completed(scope, task.id, True)
        self._remember_undo(user_id, f"completing “{task.title}”",
                            lambda: self._tasks.set_completed(scope, task.id, False))
        left = len(self._pending_tasks(scope))
        tail = f" {left} task{'s' if left != 1 else ''} left." if left else " That was your last task — nice!"
        return LocalReply(f"✔️ Marked **{task.title}** as done.{tail}",
                          speech=f"Nice, {task.title} is done.{tail}", kind="task")

    def _done(self, scope: Session, user_id: int, spec: str, explicit: bool) -> LocalReply | None:
        spec = re.sub(r"^(?:my|the|that|this)\s+(?=\w)", "", spec.strip(), flags=re.I)
        if spec.lower() in ("it", "that", "this"):
            recent = self._recent_task_reminder(user_id)
            if recent:
                return self._complete(scope, user_id, [self._tasks.get(scope, recent[0])])
        pending = self._pending_tasks(scope)
        chosen = self._match(pending, spec)
        if not chosen:
            if not explicit:
                return None
            names = "; ".join(t.title for t in pending[:6])
            return LocalReply(f"I couldn't find a task called “{spec}”." + (f" Your tasks: {names}." if names else
                                                                          " Your task list is empty."), kind="task")
        if len(chosen) > 1:
            return LocalReply("Which one? " + "; ".join(t.title for t in chosen[:6]) + ".", kind="task")
        return self._complete(scope, user_id, chosen)

    def _delete_task(self, scope: Session, user_id: int, spec: str) -> LocalReply:
        pending = self._pending_tasks(scope)
        chosen = self._match(pending, spec) or self._match(self._tasks.list(scope, True), spec)
        if not chosen:
            return LocalReply(f"I couldn't find a task called “{spec}”.", kind="task")
        if len(chosen) > 1:
            return LocalReply("More than one task matches: " + "; ".join(t.title for t in chosen[:6]) + ". Which one?",
                              kind="task")
        task = chosen[0]
        self._tasks.delete(scope, task.id)
        self._remember_undo(user_id, f"deleting “{task.title}”", lambda: self._tasks.create(
            scope, task.title, task.notes, task.priority, task.due_date, task.due_time, task.remind_at))
        return LocalReply(f"🗑️ Deleted the task **{task.title}**. Say “undo” to bring it back.",
                          speech=f"Deleted {task.title}.", kind="task")

    def _priority(self, scope: Session, user_id: int, spec: str, level: str) -> LocalReply | None:
        chosen = self._match(self._pending_tasks(scope), spec)
        if len(chosen) != 1:
            return None
        task = chosen[0]
        priority = {"top": "high", "urgent": "high", "important": "high", "normal": "medium"}.get(level.lower(),
                                                                                                level.lower())
        self._tasks.update(scope, task.id, priority=priority)
        self._remember_undo(user_id, "the priority change",
                            lambda: self._tasks.update(scope, task.id, priority=task.priority))
        return LocalReply(f"**{task.title}** is now {priority} priority.", kind="task")

    # -- reminders: put them where they belong ------------------------------------------------------------------
    def _remind(self, scope: Session, user_id: int, t: str) -> LocalReply | None:
        rest = RE_REMIND.match(t).group("rest").strip()
        if re.search(r"\b(?:by|before|until|till|due)\b", rest, re.I) and not re.search(
                r"\b(?:minutes?|hours?|mins?) before\b", rest, re.I):
            # a to-do with a deadline -> TASKS, with its own reminder
            data = self._parse_task(rest)
            if data["due_date"] is not None:
                return self._add_task(scope, user_id, rest, data)
            return None
        when = parse_when(t, self._clock())
        if when is None or not when.title:
            return None
        chosen = self._match(self._pending_tasks(scope), when.title)
        if len(chosen) == 1 and when.at > self._clock():
            task = chosen[0]
            self._tasks.update(scope, task.id, remind_at=when.at)
            self._remember_undo(user_id, "the reminder", lambda: self._tasks.update(scope, task.id,
                                                                                    remind_at=task.remind_at))
            at = describe(when.at, self._clock())
            return LocalReply(f"⏰ Okay, I'll remind you to **{task.title}** {at}.\n\n_Saved on that task in TASKS._",
                              speech=f"Okay, I'll remind you to {task.title} {at}.", kind="task")
        return None   # an ordinary reminder: saved on the SCHEDULE page by EverydayAssistant

    # -- schedule -------------------------------------------------------------------------------------------------
    def _add_event(self, scope: Session, x: str, explicit: bool) -> LocalReply | None:
        text = " " + x + " "
        reminder: int | None = DEFAULT_EVENT_REMINDER
        if re.search(r"\b(?:no reminder|without (?:a )?reminder|don'?t remind me)\b", text, re.I):
            reminder = None
        bm = re.search(r",?\s*(?:and )?remind me (?P<n>\d+|an?|one|two|half an?|thirty|fifteen|ten|five) ?(?P<u>minutes?|"
                       r"mins?|hours?|hrs?|days?) (?:before|earlier|ahead)\b", text, re.I)
        if bm:
            n = {"a": 1, "an": 1, "one": 1, "two": 2, "half a": 0.5, "half an": 0.5, "thirty": 30, "fifteen": 15,
                 "ten": 10, "five": 5}.get(bm.group("n").lower())
            n = n if n is not None else int(bm.group("n"))
            unit = bm.group("u").lower()
            reminder = int(n if unit.startswith("min") else n * 60 if unit.startswith("h") else n * 1440)
            text = text[:bm.start()] + " " + text[bm.end():]
        text = re.sub(r",?\s*(?:and )?(?:no reminder|without (?:a )?reminder|don'?t remind me)\b", " ", text,
                      flags=re.I)
        text = re.sub(r",?\s*(?:and )?remind me\b.*$", " ", text, flags=re.I)
        when = parse_when(text, self._clock())
        if when is None:
            if not explicit:
                return None   # "I have a meeting" without a time is just conversation
            title = _event_title(text)
            self._set_pending(scope, "event_time", title=title, reminder=reminder)
            return LocalReply(f"Sure — when is **{title}**? (e.g. “tomorrow at 3 pm”, “on Friday at 10”)",
                              speech=f"Sure, when is {title}?", kind="schedule", listen_after=True)
        title = _event_title(when.title)
        if not title:
            return None
        if not when.exact and not explicit:
            return None
        return self._create_event(scope, title, when.at, reminder)

    def _create_event(self, scope: Session, title: str, at: datetime, reminder: int | None) -> LocalReply:
        now = self._clock()
        if at <= now:
            return LocalReply("That time has already passed. When is it?", kind="schedule")
        event = self._schedule.create(scope, title, at, description="Added by voice", reminder_minutes=reminder)
        user_id = self._user(scope)
        self._remember_undo(user_id, f"adding “{event.title}”", lambda: self._schedule.delete(scope, event.id))
        when = describe(at, now)
        rem = ("" if reminder is None else f" I'll remind you {reminder} minutes before." if reminder < 60 else
               f" I'll remind you {reminder // 60} hour{'s' if reminder >= 120 else ''} before." if reminder % 60 == 0
               else f" I'll remind you {reminder} minutes before.")
        if reminder == 0:
            rem = " I'll remind you when it starts."
        clash = [e for e in self._schedule.for_day(scope, at.date())
                 if e.id != event.id and abs((e.starts_at - at).total_seconds()) < EVENT_HOURS * 3600]
        warn = f"\n\n⚠️ Close to **{clash[0].title}** at {_clock(clash[0].starts_at)}." if clash else ""
        return LocalReply(f"📅 Added to your **SCHEDULE**: **{event.title}** {when}.{rem}{warn}",
                          speech=f"Added {event.title} to your schedule {when}.{rem}" +
                                 (f" Heads up, it's close to {clash[0].title}." if clash else ""), kind="schedule")

    def _events(self, scope: Session, start: datetime, end: datetime) -> list:
        days = []
        d = start.date()
        while d < end.date() or (d == end.date() and end.time() > time.min):
            days.extend(self._schedule.for_day(scope, d))
            d += timedelta(days=1)
        return [e for e in days if start <= e.starts_at < end]

    def _span(self, when: str | None) -> tuple[datetime, datetime, str]:
        now = self._clock()
        today = datetime.combine(now.date(), time.min)
        w = (when or "today").lower().strip()
        if w in ("today", "tonight"):
            return today, today + timedelta(days=1), w
        if w == "tomorrow":
            return today + timedelta(days=1), today + timedelta(days=2), "tomorrow"
        if w == "this week":
            return today, today + timedelta(days=7 - now.weekday()), "this week"
        if w == "next week":
            start = today + timedelta(days=7 - now.weekday())
            return start, start + timedelta(days=7), "next week"
        if w == "this weekend":
            start = today + timedelta(days=(5 - now.weekday()) % 7)
            return start, start + timedelta(days=2), "this weekend"
        day = w.replace("on ", "")
        days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        ahead = (days.index(day) - now.weekday()) % 7
        start = today + timedelta(days=ahead)
        return start, start + timedelta(days=1), f"on {day.capitalize()}"

    @staticmethod
    def _is_reminder(event) -> bool:
        return event.reminder_minutes == 0 and event.description in ("Set by DUMBOO",)

    def _event_line(self, event, now: datetime, with_day: bool) -> str:
        day = f"{event.starts_at.strftime('%a %d %b')} " if with_day else ""
        icon = "⏰" if self._is_reminder(event) else "📅"
        what = f"reminder: {event.title}" if self._is_reminder(event) else event.title
        past = " _(done)_" if event.starts_at < now else ""
        return f"- {icon} **{day}{_clock(event.starts_at)}** — {what}{past}"

    def _list_schedule(self, scope: Session, when: str | None) -> LocalReply:
        now = self._clock()
        start, end, label = self._span(when)
        events = self._events(scope, start, end)
        upcoming = [e for e in events if e.starts_at >= now]
        if not events:
            return LocalReply(f"Nothing on your schedule {label}. Say “schedule … at …” to add something.",
                              kind="schedule")
        many_days = (end - start).days > 1
        lines = [self._event_line(e, now, many_days) for e in events[:20]]
        spoken = [f"{'' if not many_days else e.starts_at.strftime('%A') + ' '}at {_clock(e.starts_at)}, "
                  f"{'reminder to ' if self._is_reminder(e) else ''}{e.title}" for e in (upcoming or events)[:5]]
        head = f"**Your schedule {label}** ({len(events)}):"
        speech = f"{label.capitalize()} you have {len(upcoming or events)} thing{'s' if len(upcoming or events) != 1 else ''}" \
                 f"{' left' if upcoming and len(upcoming) < len(events) else ''}: " + "; ".join(spoken) + "."
        if not upcoming:
            speech = f"Everything on your schedule {label} is already over."
        return LocalReply(head + "\n\n" + "\n".join(lines), speech=speech, kind="schedule")

    def _agenda(self, scope: Session, when: str | None) -> LocalReply:
        now = self._clock()
        start, end, label = self._span(when)
        is_today = start.date() == now.date() and (end - start).days == 1
        events = [e for e in self._events(scope, start, end) if not is_today or e.starts_at >= now]
        tasks = self._pending_tasks(scope)
        due = [t for t in tasks if t.due_date and start.date() <= t.due_date < end.date()]
        overdue = [t for t in tasks if t.is_overdue(now) and t not in due] if start.date() <= now.date() else []
        undated = [t for t in tasks if t.due_date is None]
        timed: list[tuple[datetime, str, str]] = []
        for e in events:
            what = f"reminder to {e.title}" if self._is_reminder(e) else e.title
            timed.append((e.starts_at, f"{'⏰' if self._is_reminder(e) else '📅'} {what}", what))
        for t in due:
            at = t.due_at if t.due_time else None
            if at is not None:
                timed.append((at, f"✅ {t.title} (task due)", f"{t.title} is due"))
        timed.sort(key=lambda x: x[0])
        day_tasks = [t for t in due if t.due_time is None]
        lines, spoken = [], []
        many = (end - start).days > 1
        for at, shown, said in timed:
            stamp = f"{at.strftime('%a') + ' ' if many else ''}{_clock(at)}"
            lines.append(f"- **{stamp}** — {shown}")
            spoken.append(f"{'on ' + at.strftime('%A') + ' ' if many else ''}at {_clock(at)}, {said}")
        if day_tasks:
            lines.append("")
            lines.append(f"**Due {label}:** " + ", ".join(t.title for t in day_tasks))
            spoken.append(f"due {label}: " + ", ".join(t.title for t in day_tasks))
        if overdue:
            lines.append(f"**Overdue:** " + ", ".join(t.title for t in overdue[:6]))
            spoken.append(f"overdue: " + ", ".join(t.title for t in overdue[:3]))
        if undated:
            lines.append(f"**Other tasks (no date):** " + ", ".join(t.title for t in undated[:6]) +
                         (f" +{len(undated) - 6} more" if len(undated) > 6 else ""))
        if not lines:
            free = f"Nothing planned {label} — no events, reminders or tasks due."
            return LocalReply(free + " Say “add a task …” or “schedule … at …” to plan something.",
                              speech=free, kind="agenda")
        rest = "left " if is_today and len(timed) else ""
        text = f"**Your plan {label}**\n\n" + "\n".join(lines)
        if spoken:
            speech = f"Here's what you have {rest}{label}: " + "; ".join(spoken[:6]) + "."
        else:
            speech = f"Nothing with a time {label}."
        if undated:
            speech += f" Plus {len(undated)} task{'s' if len(undated) != 1 else ''} without a date, like " \
                      f"{undated[0].title}."
        return LocalReply(text, speech=speech, kind="agenda")

    def _next(self, scope: Session) -> LocalReply:
        now = self._clock()
        items: list[tuple[datetime, str]] = []
        for e in self._schedule.upcoming(scope, days=30, include_today=True):
            if e.starts_at >= now:
                items.append((e.starts_at, f"reminder to {e.title}" if self._is_reminder(e) else e.title))
                break
        for t in self._pending_tasks(scope):
            if t.due_date is not None and t.due_at >= now:
                items.append((t.due_at, f"{t.title} (task due)"))
        if not items:
            tasks = self._pending_tasks(scope)
            if tasks:
                return LocalReply(f"Nothing with a time is coming up. Your top task is **{tasks[0].title}**.",
                                  kind="agenda")
            return LocalReply("Nothing is coming up — your schedule and task list are clear.", kind="agenda")
        at, what = min(items, key=lambda x: x[0])
        return LocalReply(f"Next up: **{what}** {describe(at, now)}.", speech=f"Next up: {what}, {describe(at, now)}.",
                          kind="agenda")

    def _free(self, scope: Session, spec: str) -> LocalReply:
        now = self._clock()
        if not spec:
            spec = "now"
        when = parse_when(spec if re.match(r"^(?:in|at|on|after|tomorrow|today|tonight|next|this)\b", spec, re.I)
                          else spec, now) if spec != "now" else None
        if spec == "now":
            at, exact = now, True
        elif when is None:
            m = re.search(_RANGE, spec, re.I)
            if not m:
                return LocalReply("When do you mean? Say e.g. “am I free tomorrow at 3”.", kind="schedule")
            at, exact = None, False
            start, end, label = self._span(m.group("when"))
        else:
            at, exact = when.at, when.exact
        if at is not None and exact:
            window = timedelta(hours=EVENT_HOURS)
            clash = [e for e in self._events(scope, at - window, at + window) if not self._is_reminder(e)]
            stamp = describe(at, now) if spec != "now" else "right now"
            if not clash:
                return LocalReply(f"Yes — you're free {stamp}. Nothing on your schedule then.",
                                  speech=f"Yes, you're free {stamp}.", kind="schedule")
            e = clash[0]
            return LocalReply(f"Not quite — you have **{e.title}** at {_clock(e.starts_at)}.",
                              speech=f"Not quite, you have {e.title} at {_clock(e.starts_at)}.", kind="schedule")
        if at is not None:
            start = datetime.combine(at.date(), time.min)
            end, label = start + timedelta(days=1), describe(at, now).split(" at ")[0]
        events = [e for e in self._events(scope, start, end) if not self._is_reminder(e)]
        if not events:
            return LocalReply(f"Yes — nothing is scheduled {label}.", kind="schedule")
        return LocalReply(f"You have {len(events)} thing{'s' if len(events) != 1 else ''} {label}: " +
                          "; ".join(f"{e.title} at {_clock(e.starts_at)}" for e in events[:5]) + ".", kind="schedule")

    def _when_is(self, scope: Session, spec: str) -> LocalReply | None:
        now = self._clock()
        events = [e for e in self._schedule.upcoming(scope, days=366, include_today=True) if e.starts_at >= now]
        chosen = self._match(events, spec)
        if len(chosen) >= 1:
            e = chosen[0]
            return LocalReply(f"**{e.title}** is {describe(e.starts_at, now)}.",
                              speech=f"{e.title} is {describe(e.starts_at, now)}.", kind="schedule")
        tasks = [t for t in self._match(self._pending_tasks(scope), spec)]
        if tasks:
            t = tasks[0]
            due = self._due_text(t, now) or "not given a due date yet"
            return LocalReply(f"**{t.title}** is {due}.", speech=f"{t.title} is {due}.", kind="task")
        return None

    def _upcoming_events(self, scope: Session) -> list:
        now = self._clock()
        return [e for e in self._schedule.upcoming(scope, days=366, include_today=True) if e.starts_at >= now]

    def _cancel(self, scope: Session, user_id: int, spec: str) -> LocalReply | None:
        spec = spec.strip()
        events = self._upcoming_events(scope)
        explicit_task = bool(re.match(r"^(?:task|to-do)\b", spec, re.I))
        chosen = [] if explicit_task else self._match(events, spec)
        if not chosen and not explicit_task:
            at = parse_when(spec, self._clock())
            if at is not None and _EVENT_RE.search(spec):
                chosen = [e for e in events if abs((e.starts_at - at.at).total_seconds()) < 60 and
                          (_words(at.title) & _words(e.title) or not _words(at.title))]
        if chosen:
            if len(chosen) > 1:
                return LocalReply("More than one matches: " + "; ".join(
                    f"{e.title} ({describe(e.starts_at, self._clock())})" for e in chosen[:6]) + ". Which one?",
                    kind="schedule")
            e = chosen[0]
            self._schedule.delete(scope, e.id)
            self._remember_undo(user_id, f"cancelling “{e.title}”", lambda: self._schedule.create(
                scope, e.title, e.starts_at, e.description, e.reminder_minutes))
            return LocalReply(f"🗑️ Cancelled **{e.title}** ({describe(e.starts_at, self._clock())}). "
                              "Say “undo” to bring it back.", speech=f"Cancelled {e.title}.", kind="schedule")
        tasks = self._match(self._pending_tasks(scope), re.sub(r"^(?:task|to-do)\s+", "", spec, flags=re.I))
        if len(tasks) == 1:
            return self._delete_task(scope, user_id, tasks[0].title)
        if _EVENT_RE.search(spec) or explicit_task:
            return LocalReply(f"I couldn't find “{spec}” on your schedule or task list.", kind="schedule")
        return None

    def _new_time(self, old: datetime, spec: str) -> datetime | None:
        now = self._clock()
        when = parse_when(spec if re.match(r"^(?:in|at|on|after|tomorrow|today|tonight|next|this)\b", spec, re.I)
                          else ("at " + spec if re.match(r"^\d", spec) else spec), now)
        if when is None:
            return None
        has_clock = bool(_CLOCK.search(" " + spec + " "))
        has_day = bool(_DAY.search(" " + spec + " "))
        if has_clock and not has_day and not _RELATIVE.search(spec):
            return datetime.combine(old.date() if old.date() >= now.date() else when.at.date(), when.at.time())
        if has_day and not has_clock:
            return datetime.combine(when.at.date(), old.time())
        return when.at

    def _move(self, scope: Session, user_id: int, spec: str, target: str) -> LocalReply | None:
        now = self._clock()
        events = self._upcoming_events(scope)
        chosen = self._match(events, spec)
        if len(chosen) == 1:
            e = chosen[0]
            at = self._new_time(e.starts_at, target)
            if at is None:
                return LocalReply(f"To when should I move **{e.title}**? (e.g. “to 6 pm”, “to Friday”)", kind="schedule")
            if at <= now:
                return LocalReply("That time has already passed.", kind="schedule")
            self._schedule.update(scope, e.id, e.title, at, e.description, e.reminder_minutes)
            self._remember_undo(user_id, f"moving “{e.title}”", lambda: self._schedule.update(
                scope, e.id, e.title, e.starts_at, e.description, e.reminder_minutes))
            to = re.sub(r"^on ", "", describe(at, now))   # "to Tuesday at 10:30 AM", not "to on Tuesday"
            return LocalReply(f"📅 Moved **{e.title}** to {to}.", speech=f"Moved {e.title} to {to}.", kind="schedule")
        tasks = self._match(self._pending_tasks(scope), spec)
        if len(tasks) == 1:
            t = tasks[0]
            base = t.due_at or datetime.combine(now.date(), time(9, 0))
            at = self._new_time(base, target)
            if at is None:
                return None
            has_clock = bool(_CLOCK.search(" " + target + " ")) or (t.due_time is not None)
            remind = None
            if t.remind_at and t.due_at:
                remind = at - (t.due_at - t.remind_at)
            elif has_clock:
                remind = at - TASK_REMIND_BEFORE
            else:
                remind = datetime.combine(at.date(), time(9, 0))
            if remind is not None and remind <= now:
                remind = at if at > now else None
            self._tasks.update(scope, t.id, due_date=at.date(), due_time=at.time() if has_clock else None,
                               remind_at=remind)
            self._remember_undo(user_id, f"moving “{t.title}”", lambda: self._tasks.update(
                scope, t.id, due_date=t.due_date, due_time=t.due_time, remind_at=t.remind_at))
            moved = self._tasks.get(scope, t.id)
            return LocalReply(f"✅ **{t.title}** is now {self._due_text(moved, now)}." +
                              (f" I'll remind you {describe(moved.remind_at, now)}." if moved.remind_at else ""),
                              speech=f"{t.title} is now {self._due_text(moved, now)}.", kind="task")
        if len(chosen) > 1 or len(tasks) > 1:
            names = [e.title for e in chosen] + [t.title for t in tasks]
            return LocalReply("Which one? " + "; ".join(names[:6]) + ".", kind="schedule")
        return None

    # -- undo -----------------------------------------------------------------------------------------------------
    def _remember_undo(self, user_id: int, what: str, action: Callable[[], Any]) -> None:
        with self._lock:
            self._undo[user_id] = (what, action)

    def _undo_last(self, user_id: int) -> LocalReply | None:
        with self._lock:
            last = self._undo.pop(user_id, None)
        if last is None:
            return None
        what, action = last
        action()
        return LocalReply(f"↩️ Undone — I reversed {what}.", kind="task")

    # -- AI understanding for phrasings the rules don't cover --------------------------------------------------------
    def _ai_command(self, scope: Session, user_id: int, text: str) -> LocalReply | None:
        now = self._clock()
        prompt = (
            "Turn the user's message to their personal assistant into ONE action on their own task list, schedule "
            f"or reminders. Current local time: {now.strftime('%A %Y-%m-%d %H:%M')}. Understand slang, text-speak "
            "and Hinglish. Reply with JSON only: {\"action\": \"add_task|complete_task|delete_task|move|list_tasks|"
            "add_event|cancel|list_schedule|agenda|remind|none\", \"title\": \"the task, event or reminder in plain "
            "words, or empty\", \"when\": \"YYYY-MM-DDTHH:MM local time, or null\", \"time_given\": true/false "
            "(false if only a day was given), \"priority\": \"low|medium|high\", \"range\": \"today|tomorrow|this "
            "week|null\"}. Use \"none\" if the message is a general question or not about managing the user's own "
            "tasks, schedule or reminders (e.g. 'how do I schedule a meeting in Outlook').")
        try:
            raw = self._generate(prompt, text[:300], max_output_tokens=200)
            data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        except Exception:
            log.info("AI could not read an agenda command")
            return None
        action = str(data.get("action") or "none").lower()
        title = " ".join(str(data.get("title") or "").split())[:200]
        at = None
        if data.get("when"):
            try:
                at = datetime.fromisoformat(str(data["when"]))
            except ValueError:
                at = None
        if at is not None and not now - timedelta(days=1) < at < now + timedelta(days=366):
            at = None
        exact = bool(data.get("time_given", True))
        rng = data.get("range") if data.get("range") in ("today", "tomorrow", "this week") else None
        if action == "list_tasks":
            return self._list_tasks(scope, rng)
        if action == "list_schedule":
            return self._list_schedule(scope, rng)
        if action == "agenda":
            return self._agenda(scope, rng)
        if not title:
            return None
        if action == "add_task":
            parsed = {"title": _task_title(title), "priority": data.get("priority") if data.get("priority") in
                      ("low", "medium", "high") else "medium", "due_date": at.date() if at else None,
                      "due_time": at.time() if at and exact else None, "remind_at": None}
            if at is not None:
                due_at = datetime.combine(at.date(), at.time() if exact else time(9, 0))
                parsed["remind_at"] = (due_at - TASK_REMIND_BEFORE if exact and due_at - TASK_REMIND_BEFORE > now
                                       else due_at if due_at > now else None)
            return self._add_task(scope, user_id, title, parsed)
        if action == "complete_task":
            return self._done(scope, user_id, title, explicit=True)
        if action == "delete_task":
            return self._delete_task(scope, user_id, title)
        if action == "cancel":
            return self._cancel(scope, user_id, title)
        if action == "add_event" and at is not None:
            return self._create_event(scope, _event_title(title), at, DEFAULT_EVENT_REMINDER)
        if action == "move" and at is not None:
            return self._move(scope, user_id, title, at.strftime("%A %d %B at %H:%M"))
        if action == "remind" and at is not None and at > now:
            chosen = self._match(self._pending_tasks(scope), title)
            if len(chosen) == 1:
                self._tasks.update(scope, chosen[0].id, remind_at=at)
                return LocalReply(f"⏰ Okay, I'll remind you to **{chosen[0].title}** {describe(at, now)}.",
                                  kind="task")
            event = self._schedule.create(scope, _cap(title), at, description="Set by DUMBOO", reminder_minutes=0)
            return LocalReply(f"⏰ Okay, I'll remind you to **{event.title}** {describe(at, now)}.\n\n"
                              "_Saved on the SCHEDULE page._",
                              speech=f"Okay, I'll remind you to {event.title} {describe(at, now)}.", kind="reminder")
        return None


__all__ = ["AgendaAssistant", "date"]
