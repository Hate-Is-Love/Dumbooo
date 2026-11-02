"""Automation task history, stored per user in the existing SQLite database.

Like every Module 3 service, each call needs a ``Session``; the user id comes
from ``AuthService.require`` and every statement is filtered by it, so a user
can never read, change or delete another user's tasks.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Callable

from automation.models import Task
from automation.safety import safe_text
from services.auth_service import AuthService, Session
from storage.local_database import LocalDatabase

log = logging.getLogger(__name__)

UNFINISHED = ("pending", "awaiting_confirmation", "running")


class TaskNotFound(LookupError):
    user_message = "That automation task was not found."

    def __str__(self) -> str:
        return self.user_message


class AutomationHistory:
    def __init__(self, db: LocalDatabase, auth: AuthService, clock: Callable[[], datetime] = datetime.now) -> None:
        self._db = db
        self._auth = auth
        self._clock = clock

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    @staticmethod
    def _steps_json(task: Task, private: Callable[[str], tuple[str, ...]]) -> str:
        from automation.safety import safe_args

        steps = []
        for step in task.steps:
            item = step.summary()
            item["args"] = safe_args(step.capability, step.args, private(step.capability))
            item["message"] = safe_text(item["message"], 400)
            item["error"] = safe_text(item["error"], 400)
            item["verification"] = safe_text(item["verification"], 300)
            steps.append(item)
        return json.dumps(steps, ensure_ascii=False)

    # -- writing (called by the automation controller with the task owner's session) ---------
    def create(self, session: Session, task: Task, private: Callable[[str], tuple[str, ...]]) -> int:
        user = self._auth.require(session)
        now = self._now()
        return self._db.execute(
            "INSERT INTO automation_tasks (user_id, request, title, source, origin, status, confirmation, steps_json, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user.id, safe_text(task.request, 500), safe_text(task.title, 120), task.source, task.origin,
             task.status.value, task.confirmation, self._steps_json(task, private), now, now))

    def update(self, user_id: int, task: Task, private: Callable[[str], tuple[str, ...]]) -> None:
        """Progress updates from the executor thread (the task already belongs to ``user_id``)."""
        if task.id is None:
            return
        self._db.execute(
            "UPDATE automation_tasks SET status = ?, confirmation = ?, steps_json = ?, result_summary = ?, error = ?, "
            "undo_json = ?, started_at = ?, finished_at = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (task.status.value, task.confirmation, self._steps_json(task, private), safe_text(task.summary, 2000),
             safe_text(task.error, 500), json.dumps(task.undo[:500]), task.started_at, task.finished_at, self._now(),
             task.id, user_id))

    def recover_interrupted(self) -> int:
        """Startup: tasks that were running when DUMBOO closed are marked as interrupted, not completed."""
        rows = self._db.query(f"SELECT id, steps_json FROM automation_tasks WHERE status IN "
                              f"({','.join('?' * len(UNFINISHED))})", UNFINISHED)
        for row in rows:
            try:
                steps = json.loads(row["steps_json"])
            except json.JSONDecodeError:
                steps = []
            done = any(s.get("status") == "completed" for s in steps)
            for s in steps:
                if s.get("status") in ("pending", "running", "awaiting_confirmation"):
                    s["status"] = "cancelled"
            self._db.execute("UPDATE automation_tasks SET status = ?, steps_json = ?, error = ?, finished_at = ?, "
                             "updated_at = ? WHERE id = ?",
                             ("partially_completed" if done else "cancelled", json.dumps(steps),
                              "DUMBOO was closed before this task finished.", self._now(), self._now(), row["id"]))
        if rows:
            log.info("Marked %d unfinished automation task(s) as interrupted", len(rows))
        return len(rows)

    # -- reading (always the signed-in user's own tasks) ----------------------------------------
    def list(self, session: Session, limit: int = 100, status: str | None = None) -> list[dict]:
        user = self._auth.require(session)
        sql = ("SELECT id, request, title, source, origin, status, confirmation, steps_json, result_summary, error, "
               "workflow_name, created_at, started_at, finished_at FROM automation_tasks WHERE user_id = ?")
        params: list = [user.id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        rows = self._db.query(sql + " ORDER BY id DESC LIMIT ?", (*params, int(limit)))
        for row in rows:
            row["steps"] = json.loads(row.pop("steps_json") or "[]")
        return rows

    def get(self, session: Session, task_id: int) -> dict:
        user = self._auth.require(session)
        row = self._db.row("SELECT * FROM automation_tasks WHERE id = ? AND user_id = ?", (int(task_id), user.id))
        if row is None:
            raise TaskNotFound()
        row["steps"] = json.loads(row.pop("steps_json") or "[]")
        row["undo"] = json.loads(row.pop("undo_json") or "[]")
        row.pop("user_id", None)
        return row

    def last_resumable(self, session: Session) -> dict | None:
        """The most recent task that stopped before all of its steps ran."""
        user = self._auth.require(session)
        row = self._db.row("SELECT id FROM automation_tasks WHERE user_id = ? AND status IN "
                           "('partially_completed','failed','cancelled') AND origin != 'resume' "
                           "ORDER BY id DESC LIMIT 1", (user.id,))
        return self.get(session, row["id"]) if row else None

    def last_with_undo(self, session: Session) -> dict | None:
        user = self._auth.require(session)
        row = self._db.row("SELECT id FROM automation_tasks WHERE user_id = ? AND undo_json != '[]' "
                           "ORDER BY id DESC LIMIT 1", (user.id,))
        return self.get(session, row["id"]) if row else None

    def last_completed(self, session: Session) -> dict | None:
        user = self._auth.require(session)
        row = self._db.row("SELECT id FROM automation_tasks WHERE user_id = ? AND status IN "
                           "('completed','partially_completed') AND origin != 'workflow_save' "
                           "ORDER BY id DESC LIMIT 1", (user.id,))
        return self.get(session, row["id"]) if row else None

    def clear_undo(self, session: Session, task_id: int) -> None:
        user = self._auth.require(session)
        self._db.execute("UPDATE automation_tasks SET undo_json = '[]' WHERE id = ? AND user_id = ?",
                         (int(task_id), user.id))

    # -- workflows ------------------------------------------------------------------------------
    def save_workflow(self, session: Session, task_id: int, name: str) -> None:
        user = self._auth.require(session)
        with self._db.transaction() as conn:
            conn.execute("UPDATE automation_tasks SET workflow_name = NULL WHERE user_id = ? AND workflow_name = ?",
                         (user.id, name))
            changed = conn.execute("UPDATE automation_tasks SET workflow_name = ? WHERE id = ? AND user_id = ?",
                                   (name, int(task_id), user.id)).rowcount
        if not changed:
            raise TaskNotFound()

    def workflow(self, session: Session, name: str) -> dict | None:
        user = self._auth.require(session)
        row = self._db.row("SELECT id FROM automation_tasks WHERE user_id = ? AND workflow_name = ?", (user.id, name))
        return self.get(session, row["id"]) if row else None

    def forget_workflow(self, session: Session, name: str) -> bool:
        user = self._auth.require(session)
        return bool(self._db.execute("UPDATE automation_tasks SET workflow_name = NULL WHERE user_id = ? "
                                     "AND workflow_name = ?", (user.id, name)))

    def delete(self, session: Session, task_id: int) -> None:
        user = self._auth.require(session)
        if not self._db.execute("DELETE FROM automation_tasks WHERE id = ? AND user_id = ? AND status NOT IN "
                                "('pending','running','awaiting_confirmation')", (int(task_id), user.id)):
            raise TaskNotFound()

    def clear(self, session: Session, confirmed: bool = False) -> int:
        if not confirmed:
            raise ValueError("Clearing the automation history must be confirmed.")
        user = self._auth.require(session)
        return self._db.execute("DELETE FROM automation_tasks WHERE user_id = ? AND status NOT IN "
                                "('pending','running','awaiting_confirmation')", (user.id,))

    def export(self, user_id: int) -> list[dict]:
        """For the user's own data export (called by HistoryService.export with the verified user id)."""
        rows = self._db.query("SELECT request, title, source, status, steps_json, result_summary, error, workflow_name, "
                              "created_at, finished_at FROM automation_tasks WHERE user_id = ? ORDER BY id", (user_id,))
        for row in rows:
            row["steps"] = json.loads(row.pop("steps_json") or "[]")
        return rows
