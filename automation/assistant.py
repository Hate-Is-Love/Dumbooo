"""Connects automation to the existing conversation (voice and text use the same path).

``AutomationAssistant`` implements ``ConversationHooks`` and wraps Module 3's
``MemoryAssistant``: for every message (typed or spoken) it

1. leaves sign-in handling and memory commands ("remember …", "forget …") to
   the memory assistant;
2. answers "yes"/"no" when an automation step is waiting for confirmation;
3. handles "stop everything" (emergency stop);
4. turns automation requests into a task (rules first, the AI planner only for
   goals the rules don't cover), runs it through the controller and replies
   with the *verified* outcome. Tasks that take longer than a few seconds keep
   running; their result is announced when they finish.

Anything that is not an automation request goes to the AI conversation as before.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable

from ai.controller import ConversationHooks, LocalReply
from ai.memory_commands import parse_command
from app.event_bus import EventBus, Events
from automation import intents
from automation.controller import AutomationController
from automation.models import Step, StepStatus, Task, TaskStatus
from automation.planner import Planner
from automation.registry import ExecContext
from services.auth_service import AuthError, AuthService, Session

log = logging.getLogger(__name__)

YES = re.compile(r"^(?:yes|yeah|yep|yup|sure|ok(?:ay)?|confirm(?:ed)?|approve(?:d)?|go ahead|do it|proceed|"
                 r"please do|yes please|run it|continue)\b", re.I)
NO = re.compile(r"^(?:no|nope|cancel|deny|don'?t|do not|stop|never ?mind|abort|skip it)\b", re.I)
RESUME = re.compile(r"^(?:continue|resume|finish|pick up|carry on with) (?:the |my |our )?(?:last |previous |unfinished |"
                    r"interrupted )?(?:task|work|job)(?: (?:from|of) (?:yesterday|earlier|before|last time|today))?$|"
                    r"^continue where (?:we|you) left off$", re.I)
WORKFLOW = re.compile(r"^(?:use|run|start|do|repeat|apply) (?:my |the )?(?:usual |saved |normal )?(?P<name>.+?) "
                      r"(?:workflow|setup|routine)$", re.I)
UNDO_BARE = re.compile(r"^(?:undo|undo that|undo it)$", re.I)
AUTOMATION_NOTE = (
    "\n\n## Laptop automation\nDUMBOO performs laptop actions only through its automation module, which handles "
    "commands like “open Chrome” or “organise my Downloads” before you see them. You (the AI) cannot perform, and must "
    "never claim to have performed, any action on the computer. If the user seems to want an action, tell them to say "
    "it as a direct command.")


class AutomationAssistant(ConversationHooks):
    def __init__(self, bus: EventBus, auth: AuthService, controller: AutomationController, planner: Planner,
                 memory_hooks: ConversationHooks, speak: Callable[[str], None] | None = None) -> None:
        self._bus = bus
        self._auth = auth
        self._controller = controller
        self._planner = planner
        self._memory = memory_hooks
        self._speak = speak
        self._last_paths: dict[str, list[str]] = {}
        self._detached: set[int] = set()
        self._lock = threading.Lock()
        bus.subscribe(Events.AUTOMATION_TASK, self._on_task)

    # -- hooks ------------------------------------------------------------------------------
    def prompt_options(self, scope: Any) -> dict:
        return self._memory.prompt_options(scope)

    def extra_context(self, scope: Any, text: str, seq: int) -> str:
        return (self._memory.extra_context(scope, text, seq) or "") + AUTOMATION_NOTE

    def forget_session(self, scope: Session | None) -> None:
        if scope is None:
            return
        with self._lock:
            self._last_paths.pop(scope.token_hash, None)
        # The session is already revoked here, so its tasks are found by the session hash, not by validating it.
        self._controller.cancel_session(scope.token_hash, "signed out")

    def local_reply(self, scope: Any, text: str, source: str) -> LocalReply | None:
        if not isinstance(scope, Session):
            return self._memory.local_reply(scope, text, source)
        try:
            user = self._auth.require(scope)
        except AuthError:
            return self._memory.local_reply(scope, text, source)
        t = intents.clean(text)
        try:
            reply = self._automation_reply(scope, user, t, source)
        except AuthError as exc:
            return LocalReply(exc.user_message, kind="auth")
        except Exception:
            log.exception("Automation request failed")
            return LocalReply("Something went wrong while preparing that action. Nothing was changed. Details are "
                              "in the log file.", kind="automation")
        if reply is not None:
            return reply
        return self._memory.local_reply(scope, text, source)

    # -- routing ------------------------------------------------------------------------------
    def _ctx(self, session: Session, user) -> ExecContext:
        return ExecContext(session=session, user=user, services=self._controller.services, cancel=threading.Event())

    def _automation_reply(self, session: Session, user, t: str, source: str) -> LocalReply | None:
        waiting = self._controller.awaiting(user.id)
        if waiting is not None:
            if YES.match(t):
                return self._decide(session, waiting, True)
            if NO.match(t) and not intents.is_stop(t):
                return self._decide(session, waiting, False)
        if intents.is_stop(t):
            if not self._controller.busy():
                return LocalReply("Nothing is running right now, so there was nothing to stop.", kind="automation")
            report = self._controller.emergency_stop(source)
            return LocalReply(self._stop_text(report), kind="automation")
        memory = parse_command(t)
        if memory.kind != "none" and not self._automation_memory(t, memory):
            return None  # Module 3 memory command
        ctx = self._ctx(session, user)
        if intents.wants_history(t):
            return LocalReply(self._history_text(session), kind="automation")
        if RESUME.match(t):
            return self._resume(session, ctx, t, source)
        wf = WORKFLOW.match(t)
        if wf:
            return self._run_workflow(session, ctx, wf.group("name"), t, source)
        if UNDO_BARE.match(t):
            return LocalReply("Do you want to undo typing in the app you're using (say “press ctrl+z”), or undo my "
                              "last file operation (say “undo the last file operation”)?", kind="automation")
        with self._lock:
            last_paths = list(self._last_paths.get(session.token_hash, []))
        result = intents.parse(t, ctx, last_paths)
        if result is None and intents.looks_like_automation(t):
            planned = self._planner.plan(ctx, t)
            if planned.kind == "plan":
                result = intents.Plan(planned.steps, planned.title, origin="planner")
            elif planned.kind in ("clarify", "error"):
                return LocalReply(planned.text, kind="automation")
        if result is None:
            return None
        if isinstance(result, (intents.Ask, intents.Info)):
            return LocalReply(result.text, kind="automation")
        return self._run(session, result.steps, t, source, result.origin, result.title)

    @staticmethod
    def _automation_memory(t: str, memory) -> bool:
        """Memory-like sentences that belong to automation (project paths, workflows, deleting files)."""
        if re.match(r"^remember (?:that )?(?:(?:my|the|our) )?[\w .'-]{1,40}? (?:project (?:is|lives)|folder is|"
                    r"(?:website|site|web ?page|link) is)\b", t, re.I):
            return True
        if re.match(r"^remember (?:that )?my (?:usual|preferred|default) \w+ (?:application|app|editor|program|browser) "
                    r"is\b", t, re.I):
            return True
        if re.match(r"^(?:remember|save) (?:this|that|the last) (?:workflow|task|routine|setup|process)\b", t, re.I):
            return True
        if memory.kind == "forget" and intents.looks_like_files(memory.arg):
            return True
        return False

    # -- running -------------------------------------------------------------------------------
    def _run(self, session: Session, steps: list[Step], request: str, source: str, origin: str,
             title: str) -> LocalReply:
        # A new request replaces a question that is still waiting for yes/no (like Module 3's confirmations).
        note = ""
        waiting = self._controller.awaiting(self._auth.require(session).id)
        if waiting is not None:
            self._controller.confirm(session, waiting.id, False)
            self._controller.wait(waiting, 3)
            note = f"_(I cancelled “{waiting.title}”, which was waiting for your yes or no.)_\n\n"
        try:
            task = self._controller.submit(session, request, steps, source=source, origin=origin, title=title)
        except ValueError as exc:
            return LocalReply(str(exc), kind="automation")
        wait = self._controller.services.limit("reply_wait_s")
        task = self._controller.wait(task, wait)
        reply = self._reply_for(session, task)
        if note:
            reply.text = note + reply.text
        return reply

    def _decide(self, session: Session, task: Task, approve: bool) -> LocalReply:
        self._controller.confirm(session, task.id, approve)
        task = self._controller.wait(task, self._controller.services.limit("reply_wait_s"))
        return self._reply_for(session, task)

    def _reply_for(self, session: Session, task: Task) -> LocalReply:
        if task.status is TaskStatus.AWAITING_CONFIRMATION:
            return self._confirmation_reply(task)
        if not task.status.finished:
            with self._lock:
                self._detached.add(task.id)
            running = next((s for s in task.steps if s.status is StepStatus.RUNNING), None)
            what = running.description if running else task.title
            return LocalReply(f"⏳ Working on it: {what}. I'll tell you when it's done. Say “stop everything” to "
                              "cancel.", speech=f"Working on it. I'll tell you when it's done.", kind="automation")
        self._remember_paths(session, task)
        text = self.result_text(task)
        return LocalReply(text, speech=self.result_speech(task), kind="automation")

    def _confirmation_reply(self, task: Task) -> LocalReply:
        snap = task.snapshot()
        awaiting = snap["awaiting"] or {}
        risk = (awaiting.get("risk") or "medium").upper()
        details = (awaiting.get("details") or "").strip()
        if len(details) > 2500:
            details = details[:2500] + "\n…"
        if awaiting.get("plan"):
            text = (f"Here's my plan for “{task.title}”:\n\n{details}\n\nShall I go ahead? Say **yes** or **no** "
                    "(or use APPROVE / DENY on the AUTOMATION page).")
            speech = f"I have a plan with {len(task.steps)} steps. Shall I go ahead? Say yes or no."
        else:
            done = [s for s in task.steps if s.status is StepStatus.COMPLETED]
            before = ("Done so far:\n" + "\n".join(f"✓ {s.message or s.description}" for s in done) + "\n\n") if done \
                else ""
            text = (f"{before}⚠ **{risk} risk** — {awaiting.get('description', 'this step')}"
                    + (f"\n\n```\n{details}\n```" if details else "") +
                    "\n\nShall I do it? Say **yes** or **no** (or use APPROVE / DENY on the AUTOMATION page).")
            speech = f"This needs your confirmation: {awaiting.get('description', 'this step')}. Say yes or no."
        return LocalReply(text, speech=speech, kind="automation", listen_after=True)

    @staticmethod
    def result_text(task: Task) -> str:
        steps = task.steps
        if len(steps) == 1:
            s = steps[0]
            if s.status is StepStatus.COMPLETED:
                return s.message + (f"\n\n_✓ verified: {s.verification}_" if s.verification else "")
            if s.status is StepStatus.UNVERIFIED:
                return f"{s.message}\n\n_⚠ not verified: {s.verification or 'no confirmation available'}_"
            if s.status is StepStatus.DENIED and task.confirmation == "denied":
                return "Okay, I didn't do it."
            if s.status is StepStatus.CANCELLED and task.confirmation == "expired":
                return "I didn't get an answer in time, so I didn't do it."
            if s.status is StepStatus.CANCELLED:
                return "Stopped. " + (s.error if s.error and s.error != "stopped" else "Nothing was done.")
            return f"I couldn't do that: {s.error or task.error or 'unknown error'}"
        head = {TaskStatus.COMPLETED: f"Done: {task.title}.",
                TaskStatus.PARTIALLY_COMPLETED: f"Partly done: {task.title}.",
                TaskStatus.FAILED: f"I couldn't complete: {task.title}.",
                TaskStatus.CANCELLED: f"Cancelled: {task.title}."}.get(task.status, task.title)
        lines = [head, ""]
        for s in steps:
            if s.status is StepStatus.COMPLETED:
                body = s.message or s.description
                check = f" _(verified: {s.verification})_" if s.verification and len(s.verification) <= 140 else ""
                lines.append(f"✓ {body}{check}" if "\n" not in body else f"✓ {s.description}:{check}\n{body}")
            elif s.status is StepStatus.UNVERIFIED:
                lines.append(f"? {s.message or s.description} — not verified: {s.verification}")
            elif s.status in (StepStatus.FAILED, StepStatus.DENIED):
                lines.append(f"✗ {s.description}: {s.error}")
            elif s.status in (StepStatus.CANCELLED, StepStatus.SKIPPED):
                lines.append(f"– {s.description} (not run)")
        return "\n".join(lines)

    @staticmethod
    def result_speech(task: Task) -> str:
        if len(task.steps) == 1:
            s = task.steps[0]
            text = s.message if s.status in (StepStatus.COMPLETED, StepStatus.UNVERIFIED) else \
                AutomationAssistant.result_text(task)
            first = re.split(r"(?<=[.!?])\s|\n", text.strip())[0]
            return re.sub(r"[*`#<>]|\[.*?\]\(.*?\)", "", first)[:300]
        done = sum(1 for s in task.steps if s.status is StepStatus.COMPLETED)
        return {TaskStatus.COMPLETED: f"Done. All {done} steps completed.",
                TaskStatus.PARTIALLY_COMPLETED: f"I completed {done} of {len(task.steps)} steps. Details are on screen.",
                TaskStatus.FAILED: "I couldn't complete that. Details are on screen.",
                TaskStatus.CANCELLED: "Cancelled."}.get(task.status, "Finished.")

    def _remember_paths(self, session: Session, task: Task) -> None:
        paths: list[str] = []
        for s in task.steps:
            if s.status is StepStatus.COMPLETED:
                found = s.data.get("paths") or ([s.data["path"]] if s.data.get("path") else [])
                if found:
                    paths = [str(p) for p in found]
        if paths:
            with self._lock:
                self._last_paths[session.token_hash] = paths[:200]

    def _on_task(self, _name: str, payload: dict) -> None:
        snap = payload.get("task") or {}
        task_id = snap.get("id")
        if task_id is None or snap.get("status") not in ("completed", "partially_completed", "failed", "cancelled"):
            return
        with self._lock:
            if task_id not in self._detached:
                return
            self._detached.discard(task_id)
        task = self._controller.task(task_id)
        if task is None:
            return
        text = self.result_text(task)
        self._bus.publish(Events.AUTOMATION_ANNOUNCE, {"user_id": task.user_id, "task_id": task_id, "text": text})
        if self._speak is not None:
            try:
                self._speak(self.result_speech(task))
            except Exception:
                log.debug("Announcement speech failed", exc_info=True)

    # -- history, resume, workflows ---------------------------------------------------------------
    @staticmethod
    def _stop_text(report: dict) -> str:
        parts = [f"🛑 Emergency stop: {report['tasks']} task(s) stopped."]
        if report["completed"]:
            parts.append("Already completed (not undone): " + "; ".join(report["completed"][:8]) + ".")
        if report["cancelled"]:
            parts.append("Cancelled: " + "; ".join(report["cancelled"][:8]) + ".")
        if report.get("processes_stopped"):
            parts.append(f"Stopped {report['processes_stopped']} running command(s).")
        return "\n".join(parts)

    def _history_text(self, session: Session) -> str:
        rows = self._controller.services.history.list(session, limit=8)
        if not rows:
            return "I haven't run any automation tasks for you yet."
        icon = {"completed": "✓", "partially_completed": "◐", "failed": "✗", "cancelled": "–", "running": "…",
                "awaiting_confirmation": "?", "pending": "…"}
        lines = [f"{icon.get(r['status'], '•')} #{r['id']} {r['title'][:70]} — {r['status'].replace('_', ' ')} "
                 f"({r['created_at'][:16].replace('T', ' ')})" for r in rows]
        return "Your recent automation tasks:\n" + "\n".join(lines) + "\n\nThe AUTOMATION page shows every step."

    def _steps_from_record(self, record: dict, statuses: tuple[str, ...]) -> tuple[list[Step], list[str]]:
        steps, problems = [], []
        registry = self._controller.registry
        for s in record["steps"]:
            if s.get("status") not in statuses:
                continue
            cap = registry.get(s["capability"])
            args = s.get("args") or {}
            if cap is None or cap.name == "files.organize_apply":
                if cap is not None:
                    problems.append(f"{s['description']} (ask me to organise again; I'll re-check the folder)")
                continue
            hidden = [k for k, v in args.items() if isinstance(v, str) and (v.endswith("not stored]") or
                                                                            v == "[redacted]" or "… (" in v)]
            if hidden:
                problems.append(f"{s['description']} (it used private text that I don't store)")
                continue
            steps.append(Step(s["capability"], dict(args), s.get("description") or ""))
        return steps, problems

    def _resume(self, session: Session, ctx: ExecContext, t: str, source: str) -> LocalReply:
        record = self._controller.services.history.last_resumable(session)
        if record is None:
            return LocalReply("There's no unfinished task to continue.", kind="automation")
        done = [s["description"] for s in record["steps"] if s.get("status") == "completed"]
        steps, problems = self._steps_from_record(
            record, ("pending", "cancelled", "skipped", "failed", "denied", "unverified", "awaiting_confirmation",
                     "running"))
        when = record["created_at"][:16].replace("T", " ")
        intro = f"Last unfinished task (#{record['id']}, {when}): “{record['title']}”."
        if done:
            intro += " Completed then: " + "; ".join(done[:6]) + " — I won't assume those are still in place; I'll " \
                     "check again before each step."
        if not steps:
            return LocalReply(intro + "\n\nNothing from it can be continued automatically" +
                              (": " + "; ".join(problems) if problems else ".") , kind="automation")
        reply = self._run(session, steps, f"Continue task #{record['id']}: {record['title']}", source, "resume",
                          f"Continue: {record['title']}"[:80])
        note = ("\n\nNot included: " + "; ".join(problems)) if problems else ""
        return LocalReply(intro + "\n\n" + reply.text + note, speech=reply.speech, kind="automation")

    def _run_workflow(self, session: Session, ctx: ExecContext, name: str, t: str, source: str) -> LocalReply:
        from automation.personalization import saved_workflow

        found = saved_workflow(ctx, name)
        if found is None:
            return LocalReply(f"I don't have a saved workflow called “{name}”. Do the steps once, then say "
                              f"“remember this workflow as {name}”.", kind="automation")
        if found.options:
            names = ", ".join(m["memory_key"].removeprefix("workflow ") for m in found.options[:6])
            return LocalReply(f"Which workflow? You have: {names}.", kind="automation")
        wf_name = found.memory["memory_key"].lower().removeprefix("workflow ").strip()
        record = self._controller.services.history.workflow(session, wf_name)
        if record is None:
            return LocalReply(f"The workflow “{wf_name}” is in your memory, but its steps are no longer in the task "
                              "history. Please do it once more and save it again.", kind="automation")
        steps, problems = self._steps_from_record(record, ("completed",))
        if not steps:
            return LocalReply("That workflow has no steps I can repeat automatically.", kind="automation")
        reply = self._run(session, steps, f"Workflow: {wf_name}", source, "workflow", f"Workflow: {wf_name}")
        note = ("\n\nNot included: " + "; ".join(problems)) if problems else ""
        return LocalReply(reply.text + note, speech=reply.speech, kind="automation")
