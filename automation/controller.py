"""The automation controller: runs tasks step by step under the policy engine.

* One executor thread runs tasks one after another (a laptop has one keyboard
  and mouse; parallel automation would fight over them).
* Before **every** step: cancellation check → argument references resolved →
  ``PolicyEngine.evaluate`` (session, permission, arguments, targets, risk) →
  confirmation if required → execution with a timeout → verification.
* A step only counts as *completed* when its result was verified. A step that
  ran but could not be verified is reported as *unverified* and the task is
  not reported as completed.
* When a step fails, is refused or is cancelled, the remaining steps do not
  run. The task is *partially completed* if some steps finished.
* ``emergency_stop`` cancels the running task (including running commands),
  empties the queue and reports exactly what had already completed. Completed
  operations are not reversed.
* Every change is saved to the user's task history and published on the event
  bus for the dashboard (without session tokens or private text).
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable

from app.event_bus import EventBus, Events
from automation.models import ActionResult, Risk, Step, StepStatus, Task, TaskStatus
from automation.policy import PolicyEngine
from automation.registry import CapabilityRegistry, ExecContext
from automation.services import AutomationServices
from services.auth_service import AuthError, AuthService, Session

log = logging.getLogger(__name__)

_REF = re.compile(r"\{\{\s*step(\d+)\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
PLAN_ORIGINS = ("planner", "workflow", "resume")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class AutomationController:
    name = "automation"

    def __init__(self, bus: EventBus, auth: AuthService, registry: CapabilityRegistry, policy: PolicyEngine,
                 services: AutomationServices, clock: Callable[[], float] = time.monotonic) -> None:
        self._bus = bus
        self._auth = auth
        self.registry = registry
        self.policy = policy
        self.services = services
        self._clock = clock
        self._queue: "queue.Queue[Task | None]" = queue.Queue()
        self._tasks: dict[int, Task] = {}          # live tasks by local id
        self._sessions: dict[int, Session] = {}    # live task -> owner's session (memory only)
        self._local_ids = iter(range(1, 10**9))
        self._cv = threading.Condition()
        self._current: Task | None = None
        self._step_cancel: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False

    # -- lifecycle --------------------------------------------------------------------------------
    def start(self) -> None:
        try:
            self.services.history.recover_interrupted()
        except Exception:
            log.exception("Could not mark interrupted automation tasks")
        if self._thread is None:
            self._stopping = False
            self._thread = threading.Thread(target=self._loop, name="Automation", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stopping = True
        self.emergency_stop("shutdown")
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
        try:
            self.services.browser.stop()
        except Exception:
            log.debug("Browser stop failed", exc_info=True)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- submitting and waiting ---------------------------------------------------------------------
    def submit(self, session: Session, request: str, steps: list[Step], *, source: str = "typed",
               origin: str = "rules", title: str = "") -> Task:
        user = self._auth.require(session)
        if not steps:
            raise ValueError("A task needs at least one step.")
        limit = self.services.limit("max_steps")
        if len(steps) > limit:
            raise ValueError(f"That plan has {len(steps)} steps; the limit is {limit}.")
        task = Task(request=request, steps=steps, user_id=user.id, source=source, origin=origin,
                    title=title or request[:80], session_key=session.token_hash)
        task.plan_approved = origin not in PLAN_ORIGINS
        task.id = self.services.history.create(session, task, self.registry.private_args)
        with self._cv:
            self._tasks[task.id] = task
            self._sessions[task.id] = session
        self._publish(task)
        self._queue.put(task)
        log.info("Automation task %s queued (%d step(s), origin %s)", task.id, len(steps), origin)
        return task

    def wait(self, task: Task, timeout_s: float) -> Task:
        """Return when the task finished, needs a confirmation, or ``timeout_s`` passed."""
        deadline = self._clock() + timeout_s
        with self._cv:
            while not (task.status.finished or task.status is TaskStatus.AWAITING_CONFIRMATION):
                remaining = deadline - self._clock()
                if remaining <= 0:
                    break
                self._cv.wait(min(remaining, 0.25))
        return task

    def task(self, task_id: int) -> Task | None:
        with self._cv:
            return self._tasks.get(task_id)

    def active_tasks(self, user_id: int) -> list[Task]:
        with self._cv:
            return [t for t in self._tasks.values() if t.user_id == user_id and not t.status.finished]

    def awaiting(self, user_id: int) -> Task | None:
        with self._cv:
            for t in self._tasks.values():
                if t.user_id == user_id and t.status is TaskStatus.AWAITING_CONFIRMATION:
                    return t
        return None

    def busy(self) -> bool:
        with self._cv:
            return any(not t.status.finished for t in self._tasks.values())

    # -- user decisions ---------------------------------------------------------------------------------
    def _owned(self, session: Session, task_id: int | None) -> Task | None:
        user = self._auth.require(session)
        with self._cv:
            if task_id is None:
                candidates = [t for t in self._tasks.values() if t.user_id == user.id and not t.status.finished]
                return candidates[-1] if candidates else None
            task = self._tasks.get(task_id)
        if task is None or task.user_id != user.id:
            return None
        return task

    def confirm(self, session: Session, task_id: int | None, approve: bool) -> Task | None:
        """The user's answer to a confirmation question (voice, text or dashboard button)."""
        task = self._owned(session, task_id)
        if task is None or task.status is not TaskStatus.AWAITING_CONFIRMATION:
            return None
        with self._cv:
            task.decision = approve
            task.status = TaskStatus.RUNNING  # wait() now waits for the outcome, not the question
            task.decision_event.set()
            self._cv.notify_all()
        log.info("Automation task %s: user %s", task.id, "approved" if approve else "declined")
        return task

    def cancel(self, session: Session, task_id: int | None = None) -> Task | None:
        task = self._owned(session, task_id)
        if task is None or task.status.finished:
            return None
        self._cancel_task(task, "cancelled by the user")
        return task

    def _cancel_task(self, task: Task, reason: str) -> None:
        task.cancel_event.set()
        task.decision = False
        task.decision_event.set()
        if task is self._current and self._step_cancel is not None:
            self._step_cancel.set()
            self.services.processes.kill_all()
        if task.status is TaskStatus.PENDING:
            self._finish_unstarted(task, reason)
        with self._cv:
            self._cv.notify_all()

    def emergency_stop(self, source: str = "ui") -> dict:
        """Stop everything now. Returns (and publishes) what had completed and what was cancelled."""
        with self._cv:
            tasks = [t for t in self._tasks.values() if not t.status.finished]
        for task in tasks:
            self._cancel_task(task, f"emergency stop ({source})")
        killed = self.services.processes.kill_all()
        deadline = self._clock() + 6
        with self._cv:
            while any(not t.status.finished for t in tasks) and self._clock() < deadline:
                self._cv.wait(0.2)
        report = {"source": source, "tasks": len(tasks), "processes_stopped": killed,
                  "completed": [f"{s.description}" for t in tasks for s in t.steps if s.status is StepStatus.COMPLETED],
                  "cancelled": [f"{s.description}" for t in tasks for s in t.steps
                                if s.status in (StepStatus.CANCELLED, StepStatus.PENDING, StepStatus.RUNNING)]}
        if tasks:
            log.warning("Emergency stop (%s): %d task(s) stopped", source, len(tasks))
        self._bus.publish(Events.AUTOMATION_STOPPED, report)
        return report

    def cancel_session(self, session_key: str, reason: str = "signed out") -> None:
        """Called on sign-out: nothing keeps running for a session that has ended."""
        with self._cv:
            tasks = [t for t in self._tasks.values() if t.session_key == session_key and not t.status.finished]
        for task in tasks:
            self._cancel_task(task, reason)

    def cancel_user(self, user_id: int, reason: str = "signed out") -> None:
        """Called on sign-out: nothing keeps running for a user who is no longer signed in."""
        with self._cv:
            tasks = [t for t in self._tasks.values() if t.user_id == user_id and not t.status.finished]
        for task in tasks:
            self._cancel_task(task, reason)

    # -- executor ------------------------------------------------------------------------------------------
    def _loop(self) -> None:
        while True:
            task = self._queue.get()
            if task is None:
                break
            if task.status.finished:
                continue
            self._current = task
            try:
                self._run(task)
            except Exception:
                log.exception("Automation task %s crashed", task.id)
                task.error = "An internal error stopped this task. Details are in the log file."
                self._close(task)
            finally:
                self._current = None
                self._step_cancel = None
                self._forget_later(task)

    def _forget_later(self, task: Task) -> None:
        # Keep finished tasks briefly (for wait() and the dashboard), then drop the session reference.
        with self._cv:
            finished = [t for t in self._tasks.values() if t.status.finished]
            for old in finished[:-20]:
                self._tasks.pop(old.id, None)
            if task.status.finished:
                self._sessions.pop(task.id, None)

    def _ctx(self, task: Task, session: Session, step: Step | None, cancel: threading.Event) -> ExecContext:
        user = self._auth.require(session)
        return ExecContext(session=session, user=user, services=self.services, cancel=cancel, step=step,
                           task_id=task.id, progress=lambda m: self._progress(task, step, m))

    def _progress(self, task: Task, step: Step | None, message: str) -> None:
        if step is not None:
            step.message = message
        self._publish(task)

    def _run(self, task: Task) -> None:
        session = self._sessions.get(task.id)
        if session is None or task.cancelled:
            self._finish_unstarted(task, "cancelled before it started")
            return
        try:
            self._auth.require(session)
        except AuthError as exc:
            task.error = exc.user_message
            self._finish_unstarted(task, exc.user_message)
            return
        task.status = TaskStatus.RUNNING
        task.started_at = _now()
        self._save(task)
        if not task.plan_approved and not self._approve_plan(task, session):
            self._close(task)
            return
        index = 0
        while index < len(task.steps):
            step = task.steps[index]
            if task.cancelled:
                break
            if not self._run_step(task, session, step, index):
                break
            index += 1
        self._close(task)

    def _approve_plan(self, task: Task, session: Session) -> bool:
        """An AI-proposed (or resumed/replayed) plan is checked as a whole and shown before anything runs."""
        lines = []
        worst = Risk.LOW
        for n, step in enumerate(task.steps, 1):
            cap = self.registry.get(step.capability)
            ctx = self._ctx(task, session, step, threading.Event())
            if cap is None:
                step.status, step.error = StepStatus.DENIED, f"'{step.capability}' is not an available action."
                task.error = f"Step {n} was refused: {step.error}"
                return False
            if not _REF.search(str(step.args)):
                decision = self.policy.evaluate(session, step, ctx)
                if not decision.allowed and decision.missing and n > 1:
                    # e.g. "copy into Backup" after "create Backup": checked again right before it runs
                    step.risk = self.policy.classify(ctx, step.capability, step.args)
                elif not decision.allowed:
                    step.status, step.error = StepStatus.DENIED, decision.reason
                    task.error = f"Step {n} was refused: {decision.reason}"
                    return False
                else:
                    step.risk = decision.risk
            else:
                step.risk = self.policy.classify(ctx, step.capability, step.args)
            worst = Risk.max(worst, step.risk)
            lines.append(f"{n}. [{step.risk.value.upper()}] {step.description or cap.text_for(step.args)}")
        first = task.steps[0]
        first.confirm_details = "\n".join(lines) + ("\n\nHigh-risk steps will still ask again before they run."
                                                    if worst is Risk.HIGH else "")
        approved = self._await(task, first, plan=True)
        if approved:
            task.plan_approved = True
            for step in task.steps:
                if step.risk is not Risk.HIGH and not _REF.search(str(step.args)):
                    step.approved = True
            first.confirm_details = ""
            return True
        for step in task.steps:
            if step.status is StepStatus.PENDING:
                step.status = StepStatus.CANCELLED
        return False

    def _resolve_refs(self, task: Task, args: dict[str, Any], index: int) -> dict[str, Any]:
        def value_for(match: re.Match) -> Any:
            n, key = int(match.group(1)), match.group(2)
            if not 1 <= n <= index:
                raise ValueError(f"refers to step {n}, which has not run yet")
            source = task.steps[n - 1]
            if source.status is not StepStatus.COMPLETED or key not in source.data:
                raise ValueError(f"needs '{key}' from step {n}, which is not available")
            return source.data[key]

        def resolve(value: Any) -> Any:
            if isinstance(value, str):
                whole = _REF.fullmatch(value.strip())
                if whole:
                    return value_for(whole)
                return _REF.sub(lambda m: str(value_for(m)), value)
            if isinstance(value, list):
                return [resolve(v) for v in value]
            return value

        return {k: resolve(v) for k, v in args.items()}

    def _run_step(self, task: Task, session: Session, step: Step, index: int) -> bool:
        cap = self.registry.get(step.capability)
        cancel = threading.Event()
        self._step_cancel = cancel
        if task.cancelled:
            return False
        try:
            step.args = self._resolve_refs(task, step.args, index)
        except ValueError as exc:
            step.status, step.error = StepStatus.FAILED, f"Missing input: this step {exc}."
            return False
        ctx = self._ctx(task, session, step, cancel)
        decision = self.policy.evaluate(session, step, ctx)
        step.risk = decision.risk
        if cap is not None and not step.description:
            step.description = cap.text_for(step.args)
        if not decision.allowed:
            step.status, step.error = StepStatus.DENIED, decision.reason
            log.info("Task %s step %s refused: %s", task.id, step.capability, decision.reason)
            self._save(task)
            return False
        args = decision.args or {}
        if decision.needs_confirmation:
            if cap is not None and cap.confirm_details is not None and not step.confirm_details:
                try:
                    step.confirm_details = cap.confirm_details(ctx, args)
                except Exception as exc:
                    step.status, step.error = StepStatus.FAILED, f"Could not prepare the preview: {exc}"
                    return False
            if not self._await(task, step, plan=False):
                return False
            # Re-check after the wait: permissions, session and targets may have changed meanwhile.
            decision = self.policy.evaluate(session, step, ctx)
            if not decision.allowed:
                step.status, step.error = StepStatus.DENIED, decision.reason
                return False
            args = decision.args or {}
        step.status = StepStatus.RUNNING
        step.started_at = _now()
        step.message = ""
        self._save(task)
        result, timed_out = self._execute(cap, ctx, args, cancel)
        step.finished_at = _now()
        step.message = result.message
        step.verification = result.verification
        step.data = result.data
        step.changed = result.changed
        if result.undo:
            task.undo.extend(result.undo)
        if not result.ok:
            # Whatever already happened inside the step is described in its message.
            stopped = task.cancelled and not timed_out
            step.status = StepStatus.CANCELLED if stopped else StepStatus.FAILED
            step.error = result.message or ("stopped" if stopped else "failed")
            self._save(task)
            return False
        if not result.verified:
            step.status = StepStatus.UNVERIFIED
            self._save(task)
            return False
        step.status = StepStatus.COMPLETED
        if result.followups:
            task.steps[index + 1:index + 1] = result.followups
            max_steps = self.services.limit("max_steps") + 10
            if len(task.steps) > max_steps:
                step.status, step.error = StepStatus.FAILED, "The task grew too large and was stopped."
                return False
        self._save(task)
        return True

    def _execute(self, cap, ctx: ExecContext, args: dict, cancel: threading.Event) -> tuple[ActionResult, bool]:
        timeout = cap.timeout_s or self.services.limit("step_timeout_s")
        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["result"] = cap.handler(ctx, args)
            except Exception as exc:  # handlers report problems as results; this is a safety net
                from automation.safety import SafetyError, os_error_message

                if isinstance(exc, OSError) and not isinstance(exc, SafetyError):
                    log.warning("Capability %s: %s", cap.name, exc.__class__.__name__)
                    box["result"] = ActionResult.fail(os_error_message(exc))
                    return
                log.exception("Capability %s failed", cap.name)
                message = str(exc) if isinstance(exc, (PermissionError, ValueError, LookupError,
                                                       RuntimeError)) else "an internal error occurred"
                box["result"] = ActionResult.fail(f"{cap.name} failed: {message}")

        worker = threading.Thread(target=target, name=f"Step-{cap.name}", daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            cancel.set()
            worker.join(5)
            return ActionResult.fail(f"Timed out after {timeout:.0f} seconds and was stopped."), True
        return box.get("result") or ActionResult.fail("The action returned no result."), False

    def _await(self, task: Task, step: Step, plan: bool) -> bool:
        timeout = self.services.limit("confirmation_timeout_s")
        with self._cv:
            task.decision = None
            task.decision_event.clear()
            task.awaiting_step = step
            task.status = TaskStatus.AWAITING_CONFIRMATION
            task.confirmation = "requested"
            step.status = StepStatus.AWAITING_CONFIRMATION
            self._cv.notify_all()
        self._save(task)
        question = (f"Shall I run this plan ({len(task.steps)} step{'s' if len(task.steps) != 1 else ''})?"
                    if plan else f"Confirm: {step.description}?")
        self._bus.publish(Events.AUTOMATION_CONFIRM, {
            "user_id": task.user_id, "task_id": task.id, "step_id": step.id, "plan": plan,
            "risk": (step.risk or Risk.MEDIUM).value, "question": question, "details": step.confirm_details})
        decided = task.decision_event.wait(timeout)
        with self._cv:
            task.awaiting_step = None
            if task.cancelled:
                step.status = StepStatus.CANCELLED
                task.confirmation = "denied"
                approved = False
            elif not decided:
                step.status, step.error = StepStatus.CANCELLED, "No answer to the confirmation question in time."
                task.confirmation = "expired"
                approved = False
            elif task.decision:
                task.confirmation = "approved"
                step.status = StepStatus.PENDING
                step.approved = True
                approved = True
            else:
                step.status, step.error = StepStatus.DENIED, "You declined this step."
                task.confirmation = "denied"
                approved = False
            task.status = TaskStatus.RUNNING
            self._cv.notify_all()
        self._save(task)
        return approved

    # -- finishing ---------------------------------------------------------------------------------------------
    def _finish_unstarted(self, task: Task, reason: str) -> None:
        for step in task.steps:
            if step.status is StepStatus.PENDING:
                step.status = StepStatus.CANCELLED
        task.error = task.error or reason
        self._close(task)

    def _close(self, task: Task) -> None:
        if task.status.finished:
            return
        stopped = task.cancelled
        for step in task.steps:
            if step.status in (StepStatus.PENDING, StepStatus.RUNNING, StepStatus.AWAITING_CONFIRMATION):
                step.status = StepStatus.CANCELLED if stopped else StepStatus.SKIPPED
        statuses = [s.status for s in task.steps]
        completed = statuses.count(StepStatus.COMPLETED)
        if completed == len(statuses):
            status = TaskStatus.COMPLETED
        elif completed:
            status = TaskStatus.PARTIALLY_COMPLETED
        elif stopped or StepStatus.DENIED in statuses and task.confirmation == "denied" or \
                task.confirmation == "expired":
            status = TaskStatus.CANCELLED
        else:
            status = TaskStatus.FAILED
        if not task.error:
            problem = next((s for s in task.steps if s.status in (StepStatus.FAILED, StepStatus.DENIED,
                                                                   StepStatus.UNVERIFIED)), None)
            if problem is not None:
                task.error = problem.error or (f"Could not verify: {problem.message}"
                                               if problem.status is StepStatus.UNVERIFIED else "")
            elif stopped:
                task.error = "Stopped before it finished."
        task.summary = self.summarize(task, status)
        task.finished_at = _now()
        with self._cv:
            task.status = status
            self._cv.notify_all()
        self._save(task)
        log.info("Automation task %s finished: %s", task.id, status.value)

    @staticmethod
    def summarize(task: Task, status: TaskStatus) -> str:
        done = [s for s in task.steps if s.status is StepStatus.COMPLETED]
        unverified = [s for s in task.steps if s.status is StepStatus.UNVERIFIED]
        lines = []
        for s in done:
            lines.append(f"✓ {s.message or s.description}")
        for s in unverified:
            lines.append(f"? {s.message or s.description} (not verified: {s.verification or 'no confirmation'})")
        for s in task.steps:
            if s.status in (StepStatus.FAILED, StepStatus.DENIED):
                lines.append(f"✗ {s.description}: {s.error}")
        cancelled = [s for s in task.steps if s.status in (StepStatus.CANCELLED, StepStatus.SKIPPED)]
        if cancelled:
            lines.append(f"– Not run: {', '.join(s.description for s in cancelled[:6])}"
                         + (f" and {len(cancelled) - 6} more" if len(cancelled) > 6 else ""))
        return "\n".join(lines)

    def _save(self, task: Task) -> None:
        try:
            self.services.history.update(task.user_id, task, self.registry.private_args)
        except Exception:
            log.exception("Could not save automation task %s", task.id)
        self._publish(task)
        with self._cv:
            self._cv.notify_all()

    def _publish(self, task: Task) -> None:
        self._bus.publish(Events.AUTOMATION_TASK, {"user_id": task.user_id, "task": task.snapshot()})
