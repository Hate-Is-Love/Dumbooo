"""Research history in the existing SQLite database, strictly per user.

Every method needs the signed-in user's ``Session``; the user id comes from
``AuthService.require`` and every statement is filtered by it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Callable

from research.models import ResearchResult
from security.secrets_filter import redact_secrets
from services.auth_service import AuthService, Session
from storage.local_database import LocalDatabase

log = logging.getLogger(__name__)


class ResearchNotFound(LookupError):
    user_message = "That research record was not found."

    def __str__(self) -> str:
        return self.user_message


def _summary_short(result: ResearchResult) -> str:
    if result.summary:
        return result.summary[:600]
    if result.findings:
        return " ".join((f.headline + ": " if f.headline else "") + f.text for f in result.findings[:3])[:600]
    return ""


class ResearchHistory:
    def __init__(self, db: LocalDatabase, auth: AuthService, clock: Callable[[], datetime] = datetime.now) -> None:
        self._db = db
        self._auth = auth
        self._clock = clock

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    # -- writing ------------------------------------------------------------------------------------
    def create(self, session: Session, result: ResearchResult, source: str = "typed",
               refreshed_from: int | None = None) -> int:
        user = self._auth.require(session, "research.web")
        if refreshed_from is not None and not self._db.row(
                "SELECT 1 FROM research_sessions WHERE id = ? AND user_id = ?", (int(refreshed_from), user.id)):
            refreshed_from = None
        now = self._now()
        return self._db.execute(
            "INSERT INTO research_sessions (user_id, query, kind, status, source, refreshed_from, created_at, updated_at)"
            " VALUES (?, ?, ?, 'running', ?, ?, ?, ?)",
            (user.id, redact_secrets(result.query)[:1000], result.kind, source, refreshed_from, now, now))

    def finish(self, user_id: int, result: ResearchResult, answer: str) -> None:
        if result.id is None:
            return
        self._db.execute(
            "UPDATE research_sessions SET status = ?, summary = ?, answer = ?, findings_json = ?, sources_json = ?, "
            "plan_json = ?, notes_json = ?, period = ?, error = ?, finished_at = ?, updated_at = ? "
            "WHERE id = ? AND user_id = ?",
            (result.status, _summary_short(result), answer[:30000],
             json.dumps([f.as_dict() for f in result.findings])[:60000],
             json.dumps([s.stored() for s in result.sources])[:80000], json.dumps(result.plan)[:8000],
             json.dumps(result.notes[:20]), result.period, redact_secrets(result.error)[:500], self._now(), self._now(),
             result.id, user_id))

    def recover_interrupted(self) -> int:
        n = self._db.execute("UPDATE research_sessions SET status = 'cancelled', error = 'DUMBOO was closed before the "
                             "research finished.', updated_at = ? WHERE status = 'running'", (self._now(),))
        if n:
            log.info("Marked %d unfinished research session(s) as cancelled", n)
        return n

    # -- reading (own records only) -------------------------------------------------------------------
    @staticmethod
    def _decode(row: dict) -> dict:
        for key in ("findings_json", "sources_json", "plan_json", "notes_json"):
            if key in row:
                row[key[:-5]] = json.loads(row.pop(key) or "[]")
        row.pop("user_id", None)
        return row

    def list(self, session: Session, text: str = "", limit: int = 50) -> list[dict]:
        user = self._auth.require(session, "research.web")
        sql = ("SELECT id, query, kind, status, source, summary, period, error, refreshed_from, memory_id, user_notes, "
               "created_at, finished_at FROM research_sessions WHERE user_id = ?")
        params: list = [user.id]
        words = [w for w in text.lower().split() if len(w) > 2][:6]
        for w in words:
            sql += " AND (lower(query) LIKE ? OR lower(summary) LIKE ? OR lower(user_notes) LIKE ?)"
            params += [f"%{w}%"] * 3
        return self._db.query(sql + " ORDER BY id DESC LIMIT ?", (*params, int(limit)))

    def get(self, session: Session, research_id: int) -> dict:
        user = self._auth.require(session, "research.web")
        row = self._db.row("SELECT * FROM research_sessions WHERE id = ? AND user_id = ?", (int(research_id), user.id))
        if row is None:
            raise ResearchNotFound()
        return self._decode(row)

    def latest(self, session: Session, text: str = "", statuses: tuple[str, ...] = ("completed", "partial")) -> dict | None:
        for row in self.list(session, text, limit=30):
            if row["status"] in statuses:
                return self.get(session, row["id"])
        return None

    def saved_sources(self, session: Session, limit: int = 40) -> list[dict]:
        """Sources of research the user saved (asked DUMBOO to remember)."""
        user = self._auth.require(session, "research.web")
        out: list[dict] = []
        for row in self._db.query("SELECT id, sources_json, created_at FROM research_sessions WHERE user_id = ? AND "
                                  "memory_id IS NOT NULL ORDER BY id DESC LIMIT 20", (user.id,)):
            for s in json.loads(row["sources_json"] or "[]"):
                out.append({**s, "research_id": row["id"], "researched_at": row["created_at"]})
        return out[:limit]

    # -- user changes ---------------------------------------------------------------------------------
    def set_notes(self, session: Session, research_id: int, notes: str) -> None:
        user = self._auth.require(session, "research.web")
        if not self._db.execute("UPDATE research_sessions SET user_notes = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                                (redact_secrets(notes)[:4000], self._now(), int(research_id), user.id)):
            raise ResearchNotFound()

    def link_memory(self, session: Session, research_id: int, memory_id: int | None) -> None:
        user = self._auth.require(session, "research.web")
        self._db.execute("UPDATE research_sessions SET memory_id = ?, updated_at = ? WHERE id = ? AND user_id = ? "
                         "AND (? IS NULL OR EXISTS (SELECT 1 FROM long_term_memory WHERE id = ? AND user_id = ?))",
                         (memory_id, self._now(), int(research_id), user.id, memory_id, memory_id, user.id))

    def delete(self, session: Session, research_id: int) -> None:
        user = self._auth.require(session, "research.web")
        if not self._db.execute("DELETE FROM research_sessions WHERE id = ? AND user_id = ? AND status != 'running'",
                                (int(research_id), user.id)):
            raise ResearchNotFound()

    def clear(self, session: Session, confirmed: bool = False) -> int:
        if not confirmed:
            raise ValueError("Clearing the research history must be confirmed.")
        user = self._auth.require(session, "research.web")
        return self._db.execute("DELETE FROM research_sessions WHERE user_id = ? AND status != 'running'", (user.id,))
