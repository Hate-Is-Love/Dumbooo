"""Long-term memory: facts DUMBOO keeps about each user across restarts.

* Strictly per user: every statement is filtered by the authenticated user id.
* One active fact per key ("project", "favourite colour" …). Saying the same
  thing again changes nothing (idempotent); saying something different
  **supersedes** the old fact (kept as history, never shown as current).
* Forgetting erases the fact's text (not just hides it); only an audit entry
  without the content remains.
* Passwords, API keys, tokens and similar secrets are refused. Sensitive
  personal categories (health, religion, finances …) are only stored after
  the user confirms.
* Every change is written to ``memory_audit`` (ids, category, action — no secrets).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from security.secrets_filter import contains_secret, is_sensitive
from services.auth_service import AuthService, Session
from storage.local_database import LocalDatabase
from storage.search import fts_query, keywords

log = logging.getLogger(__name__)

CATEGORIES = ("identity", "preference", "project", "goal", "fact", "instruction", "general")
MAX_KEY = 80
MAX_VALUE = 500
CORE_CATEGORIES = ("identity", "preference", "instruction")


class MemoryError_(ValueError):  # noqa: N801
    user_message = "That memory could not be saved."

    def __init__(self, message: str | None = None) -> None:
        self.user_message = message or self.user_message
        super().__init__(self.user_message)


class MemoryRejected(MemoryError_):
    user_message = "I won't store passwords, keys or other secrets."


class NeedsConfirmation(MemoryError_):
    user_message = "That looks like sensitive personal information. Please confirm before I save it."


class MemoryNotFound(MemoryError_):
    user_message = "I couldn't find that memory."


@dataclass
class MemoryResult:
    action: str   # created | unchanged | superseded | confirmed
    memory: dict


def normalize_key(key: str) -> str:
    key = (key or "").lower().strip()
    key = re.sub(r"^(?:my|the|our)\s+", "", key)
    key = re.sub(r"[^a-z0-9 ]+", " ", key)
    key = re.sub(r"\b(?:favorite)\b", "favourite", key)
    return " ".join(key.split())


class MemoryService:
    def __init__(self, db: LocalDatabase, auth: AuthService, clock: Callable[[], datetime] = datetime.now) -> None:
        self._db = db
        self._auth = auth
        self._clock = clock

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    def _audit(self, conn: sqlite3.Connection, user_id: int, memory_id: int | None, action: str, **meta) -> None:
        conn.execute("INSERT INTO memory_audit (user_id, memory_id, action, timestamp, metadata) VALUES (?, ?, ?, ?, ?)",
                     (user_id, memory_id, action, self._now(), json.dumps(meta)))

    @staticmethod
    def _validate(key: str, value: str, category: str) -> tuple[str, str, str]:
        key = " ".join((key or "").split())
        value = " ".join((value or "").split())
        if not key or not value:
            raise MemoryError_("A memory needs a subject and a value.")
        if len(key) > MAX_KEY or len(value) > MAX_VALUE:
            raise MemoryError_(f"Memories are limited to {MAX_KEY} characters for the subject and "
                               f"{MAX_VALUE} for the value.")
        if category not in CATEGORIES:
            raise MemoryError_("Unknown memory category.")
        if contains_secret(key) or contains_secret(value):
            raise MemoryRejected()
        return key, value, category

    # -- writing -----------------------------------------------------------------------
    def remember(self, session: Session, key: str, value: str, category: str = "general", *,
                 source_message_id: int | None = None, confidence: float = 1.0,
                 verification: str = "user_confirmed", sensitive_confirmed: bool = False) -> MemoryResult:
        user = self._auth.require(session, "memory.use")
        key, value, category = self._validate(key, value, category)
        sensitive = is_sensitive(key) or is_sensitive(value)
        if sensitive and not sensitive_confirmed:
            raise NeedsConfirmation()
        key_norm = normalize_key(key) or key.lower()
        confidence = max(0.0, min(1.0, float(confidence)))
        now = self._now()
        with self._db.transaction() as conn:
            if source_message_id is not None and not conn.execute(
                    "SELECT 1 FROM messages WHERE id = ? AND user_id = ?", (int(source_message_id), user.id)).fetchone():
                source_message_id = None  # never link to someone else's message
            existing = conn.execute("SELECT * FROM long_term_memory WHERE user_id = ? AND key_norm = ? AND status = 'active'",
                                    (user.id, key_norm)).fetchone()
            if existing and existing["memory_value"].casefold() == value.casefold():
                stronger = verification == "user_confirmed" and existing["verification_status"] != "user_confirmed"
                conn.execute("UPDATE long_term_memory SET confidence = MAX(confidence, ?), verification_status = ?, "
                             "updated_at = ? WHERE id = ?",
                             (confidence, "user_confirmed" if stronger else existing["verification_status"], now,
                              existing["id"]))
                if stronger:
                    self._audit(conn, user.id, existing["id"], "confirm", category=category)
                memory = dict(conn.execute("SELECT * FROM long_term_memory WHERE id = ?", (existing["id"],)).fetchone())
                return MemoryResult("confirmed" if stronger else "unchanged", memory)
            if existing:
                conn.execute("UPDATE long_term_memory SET status = 'superseded', updated_at = ? WHERE id = ?",
                             (now, existing["id"]))
                self._audit(conn, user.id, existing["id"], "supersede", category=existing["category"])
            new_id = conn.execute(
                "INSERT INTO long_term_memory (user_id, memory_key, key_norm, memory_value, category, source_message_id, "
                "confidence, verification_status, sensitivity, supersedes_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user.id, key, key_norm, value, category, source_message_id, confidence, verification,
                 "sensitive" if sensitive else "normal", existing["id"] if existing else None, now, now)).lastrowid
            self._audit(conn, user.id, new_id, "create", category=category, verification=verification,
                        supersedes=existing["id"] if existing else None)
            memory = dict(conn.execute("SELECT * FROM long_term_memory WHERE id = ?", (new_id,)).fetchone())
        return MemoryResult("superseded" if existing else "created", memory)

    def update(self, session: Session, memory_id: int, *, key: str | None = None, value: str | None = None,
               category: str | None = None) -> dict:
        """Edit a memory from the dashboard (the user corrects it directly)."""
        user = self._auth.require(session, "memory.use")
        current = self._get_active(user.id, memory_id)
        new_key, new_value, new_category = self._validate(key if key is not None else current["memory_key"],
                                                          value if value is not None else current["memory_value"],
                                                          category if category is not None else current["category"])
        try:
            with self._db.transaction() as conn:
                conn.execute("UPDATE long_term_memory SET memory_key = ?, key_norm = ?, memory_value = ?, category = ?, "
                             "verification_status = 'user_confirmed', confidence = 1.0, sensitivity = ?, updated_at = ? "
                             "WHERE id = ? AND user_id = ?",
                             (new_key, normalize_key(new_key) or new_key.lower(), new_value, new_category,
                              "sensitive" if is_sensitive(new_key + " " + new_value) else "normal", self._now(),
                              current["id"], user.id))
                self._audit(conn, user.id, current["id"], "update", category=new_category)
        except sqlite3.IntegrityError as exc:
            raise MemoryError_("Another saved memory already uses that subject.") from exc
        return self._get_active(user.id, memory_id)

    def _get_active(self, user_id: int, memory_id: int) -> dict:
        row = self._db.row("SELECT * FROM long_term_memory WHERE id = ? AND user_id = ? AND status = 'active'",
                           (int(memory_id), user_id))
        if row is None:
            raise MemoryNotFound()
        return row

    def forget(self, session: Session, memory_id: int) -> None:
        """Erase one memory of the current user (its text is wiped, an audit entry remains)."""
        user = self._auth.require(session, "memory.use")
        with self._db.transaction() as conn:
            row = conn.execute("SELECT id, category FROM long_term_memory WHERE id = ? AND user_id = ? AND status != 'deleted'",
                               (int(memory_id), user.id)).fetchone()
            if row is None:
                raise MemoryNotFound()
            # Erase this fact and every older version it replaced.
            current: int | None = row["id"]
            seen: set[int] = set()
            while current is not None and current not in seen:
                seen.add(current)
                older = conn.execute("SELECT supersedes_id FROM long_term_memory WHERE id = ? AND user_id = ?",
                                     (current, user.id)).fetchone()
                self._erase(conn, user.id, current)
                current = older["supersedes_id"] if older else None
            self._audit(conn, user.id, row["id"], "delete", category=row["category"], versions=len(seen))

    def _erase(self, conn: sqlite3.Connection, user_id: int, memory_id: int) -> None:
        conn.execute("UPDATE long_term_memory SET status = 'deleted', memory_key = '(forgotten)', "
                     "key_norm = 'forgotten-' || id, memory_value = '', updated_at = ? WHERE id = ? AND user_id = ?",
                     (self._now(), memory_id, user_id))

    def clear_all(self, session: Session, confirmed: bool = False) -> int:
        if not confirmed:
            raise MemoryError_("Clearing all memories must be confirmed.")
        user = self._auth.require(session, "memory.use")
        with self._db.transaction() as conn:
            ids = [r["id"] for r in conn.execute("SELECT id FROM long_term_memory WHERE user_id = ? AND status != 'deleted'",
                                                 (user.id,)).fetchall()]
            for memory_id in ids:
                self._erase(conn, user.id, memory_id)
            self._audit(conn, user.id, None, "clear", count=len(ids))
        return len(ids)

    # -- reading ------------------------------------------------------------------------------
    def list(self, session: Session, include_superseded: bool = False, category: str | None = None) -> list[dict]:
        user = self._auth.require(session, "memory.use")
        statuses = ("active", "superseded") if include_superseded else ("active",)
        sql = (f"SELECT * FROM long_term_memory WHERE user_id = ? AND status IN ({','.join('?' * len(statuses))})")
        params: list = [user.id, *statuses]
        if category:
            sql += " AND category = ?"
            params.append(category)
        return self._db.query(sql + " ORDER BY status = 'active' DESC, updated_at DESC, id DESC", params)

    def search(self, session: Session, text: str, limit: int = 10) -> list[dict]:
        user = self._auth.require(session, "memory.use")
        return self._search(user.id, text, limit)

    def _search(self, user_id: int, text: str, limit: int) -> list[dict]:
        query = fts_query(text)
        if query is None:
            return []
        if self._db.fts_enabled:
            return self._db.query(
                "SELECT m.*, bm25(memory_fts, 3.0, 1.0, 0.5) AS rank FROM memory_fts "
                "JOIN long_term_memory m ON m.id = memory_fts.rowid "
                "WHERE memory_fts MATCH ? AND m.user_id = ? AND m.status = 'active' "
                "ORDER BY rank, m.updated_at DESC LIMIT ?", (query, user_id, int(limit)))
        like = [f"%{k}%" for k in keywords(text)]
        clause = " OR ".join("(memory_key LIKE ? OR memory_value LIKE ?)" for _ in like)
        params = [p for k in like for p in (k, k)]
        return self._db.query(f"SELECT *, 0 AS rank FROM long_term_memory WHERE user_id = ? AND status = 'active' "
                              f"AND ({clause}) ORDER BY updated_at DESC LIMIT ?", (user_id, *params, int(limit)))

    def retrieve_for_prompt(self, session: Session, query: str, limit: int = 6) -> list[dict]:
        """Relevant facts for answering ``query``: search matches first, then core facts, newest first.

        Only the current user's active memories are considered; secrets are never returned.
        """
        user = self._auth.require(session, "memory.use")
        if limit <= 0:
            return []
        chosen: dict[int, dict] = {}
        for row in self._search(user.id, query, limit):
            chosen.setdefault(row["id"], row)
        if len(chosen) < limit:
            for row in self._db.query(
                    "SELECT * FROM long_term_memory WHERE user_id = ? AND status = 'active' AND category IN (?, ?, ?) "
                    "ORDER BY updated_at DESC LIMIT ?", (user.id, *CORE_CATEGORIES, limit)):
                if len(chosen) >= limit:
                    break
                chosen.setdefault(row["id"], row)
        results = [r for r in chosen.values() if not contains_secret(r["memory_key"] + " " + r["memory_value"])]
        if results:
            self._db.execute(f"UPDATE long_term_memory SET last_accessed_at = ? WHERE user_id = ? AND id IN "
                             f"({','.join('?' * len(results))})", (self._now(), user.id, *[r["id"] for r in results]))
        return results

    def link_source(self, session: Session, memory_id: int, message_id: int) -> None:
        """Record which of the user's own messages a memory came from (both must belong to the user)."""
        user = self._auth.require(session, "memory.use")
        self._db.execute("UPDATE long_term_memory SET source_message_id = ? WHERE id = ? AND user_id = ? "
                         "AND source_message_id IS NULL AND EXISTS (SELECT 1 FROM messages WHERE id = ? AND user_id = ?)",
                         (int(message_id), int(memory_id), user.id, int(message_id), user.id))

    def audit(self, session: Session, limit: int = 100) -> list[dict]:
        user = self._auth.require(session, "memory.use")
        return self._db.query("SELECT memory_id, action, timestamp, metadata FROM memory_audit WHERE user_id = ? "
                              "ORDER BY id DESC LIMIT ?", (user.id, int(limit)))

    def count(self, session: Session) -> int:
        user = self._auth.require(session)
        return self._db.scalar("SELECT COUNT(*) FROM long_term_memory WHERE user_id = ? AND status = 'active'",
                               (user.id,))
