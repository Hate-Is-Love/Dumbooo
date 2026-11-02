"""Conversation history: every real message, per user, persisted in SQLite.

Rules:

* Every method takes the caller's ``Session``; the user id comes from
  ``AuthService.require`` and every SQL statement is filtered by it. Asking for
  another user's conversation simply finds nothing ("not found").
* A user message is saved when it is accepted for processing (status
  ``pending``) and later marked ``completed``, ``failed`` or ``interrupted``.
* An assistant message is saved once, when the real answer exists. A streamed
  answer is stored as one row (not one row per token). Partial text from an
  interrupted or failed answer is stored with status ``interrupted`` — never
  as ``completed``. Nothing is invented to fill a gap.
* ``request_key`` (unique per user) and the one-answer-per-message index make
  saving idempotent: repeating a save (retry, duplicate event) never duplicates rows.

Deleting a conversation deletes its messages. It does **not** delete long-term
memories that were created from it: those live on (their ``source_message_id``
becomes empty) until you delete them on the MEMORY page.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Callable

from services.auth_service import AuthService, Session
from storage.local_database import LocalDatabase
from storage.search import fts_query, keywords

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 20000


class HistoryNotFound(LookupError):
    user_message = "That conversation was not found."


class HistoryService:
    def __init__(self, db: LocalDatabase, auth: AuthService, clock: Callable[[], datetime] = datetime.now) -> None:
        self._db = db
        self._auth = auth
        self._clock = clock

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    def _owned_conversation(self, conn_or_db, user_id: int, conversation_id: int) -> dict:
        query = "SELECT * FROM conversations WHERE id = ? AND user_id = ?"
        if isinstance(conn_or_db, sqlite3.Connection):
            row = conn_or_db.execute(query, (int(conversation_id), user_id)).fetchone()
            row = dict(row) if row else None
        else:
            row = conn_or_db.row(query, (int(conversation_id), user_id))
        if row is None:
            raise HistoryNotFound()
        return row

    # -- writing (used by the conversation recorder) -----------------------------------
    def start_conversation(self, session: Session, title: str | None = None) -> int:
        user = self._auth.require(session)
        now = self._now()
        return self._db.execute("INSERT INTO conversations (user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                                (user.id, (title or "New conversation")[:80], now, now))

    def record_user_message(self, session: Session, conversation_id: int | None, content: str, input_mode: str,
                            request_key: str) -> tuple[int, int]:
        """Save an accepted user message. Returns (conversation_id, message_id). Idempotent on request_key."""
        user = self._auth.require(session)
        content = (content or "")[:MAX_MESSAGE_CHARS]
        mode = "voice" if input_mode == "voice" else "text"
        now = self._now()
        with self._db.transaction() as conn:
            existing = conn.execute("SELECT id, conversation_id FROM messages WHERE user_id = ? AND request_key = ?",
                                    (user.id, request_key)).fetchone()
            if existing:
                conn.execute("UPDATE messages SET status = 'pending', error_kind = NULL, updated_at = ? WHERE id = ?",
                             (now, existing["id"]))
                return existing["conversation_id"], existing["id"]
            if conversation_id is not None:
                self._owned_conversation(conn, user.id, conversation_id)
            else:
                title = " ".join(content.split())[:60] or "New conversation"
                conversation_id = conn.execute(
                    "INSERT INTO conversations (user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (user.id, title, now, now)).lastrowid
            message_id = conn.execute(
                "INSERT INTO messages (conversation_id, user_id, role, content, input_mode, status, request_key, "
                "created_at, updated_at) VALUES (?, ?, 'user', ?, ?, 'pending', ?, ?, ?)",
                (conversation_id, user.id, content, mode, request_key, now, now)).lastrowid
            conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
        return conversation_id, message_id

    def record_assistant_message(self, session: Session, reply_to: int, content: str, provider: str | None,
                                 model: str | None, status: str = "completed") -> int | None:
        """Save DUMBOO's real answer to ``reply_to`` and close the user message. Idempotent."""
        if status not in ("completed", "interrupted"):
            raise ValueError("assistant messages are completed or interrupted")
        user = self._auth.require(session)
        content = (content or "").strip()[:MAX_MESSAGE_CHARS]
        now = self._now()
        with self._db.transaction() as conn:
            parent = conn.execute("SELECT id, conversation_id FROM messages WHERE id = ? AND user_id = ? AND role = 'user'",
                                  (int(reply_to), user.id)).fetchone()
            if parent is None:
                raise HistoryNotFound()
            conn.execute("UPDATE messages SET status = ?, updated_at = ? WHERE id = ?",
                         ("completed" if status == "completed" else "interrupted", now, parent["id"]))
            if not content:
                return None  # nothing real to store; never fabricate an answer
            existing = conn.execute("SELECT id FROM messages WHERE reply_to = ? AND role = 'assistant'",
                                    (parent["id"],)).fetchone()
            if existing:
                conn.execute("UPDATE messages SET content = ?, status = ?, provider = ?, model = ?, updated_at = ? "
                             "WHERE id = ?", (content, status, provider, model, now, existing["id"]))
                message_id = existing["id"]
            else:
                message_id = conn.execute(
                    "INSERT INTO messages (conversation_id, user_id, role, content, status, provider, model, reply_to, "
                    "created_at, updated_at) VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?, ?, ?)",
                    (parent["conversation_id"], user.id, content, status, provider, model, parent["id"], now,
                     now)).lastrowid
            conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, parent["conversation_id"]))
        return message_id

    def mark_user_message(self, session: Session, message_id: int, status: str, error_kind: str | None = None) -> None:
        if status not in ("failed", "interrupted", "completed", "pending"):
            raise ValueError("invalid status")
        user = self._auth.require(session)
        self._db.execute("UPDATE messages SET status = ?, error_kind = ?, updated_at = ? "
                         "WHERE id = ? AND user_id = ? AND role = 'user'",
                         (status, error_kind, self._now(), int(message_id), user.id))

    # -- reading ----------------------------------------------------------------------------
    def list_conversations(self, session: Session, include_archived: bool = False, limit: int = 100) -> list[dict]:
        user = self._auth.require(session)
        status_filter = "" if include_archived else "AND c.status = 'active'"
        return self._db.query(
            f"SELECT c.id, c.title, c.status, c.created_at, c.updated_at, COUNT(m.id) AS message_count "
            f"FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id "
            f"WHERE c.user_id = ? {status_filter} GROUP BY c.id ORDER BY c.updated_at DESC, c.id DESC LIMIT ?",
            (user.id, int(limit)))

    def get_conversation(self, session: Session, conversation_id: int) -> dict:
        user = self._auth.require(session)
        conversation = self._owned_conversation(self._db, user.id, conversation_id)
        conversation["messages"] = self._db.query(
            "SELECT id, role, content, input_mode, status, provider, model, error_kind, reply_to, created_at "
            "FROM messages WHERE conversation_id = ? AND user_id = ? ORDER BY id", (conversation["id"], user.id))
        return conversation

    def messages_between(self, session: Session, start: datetime, end: datetime, limit: int = 120) -> list[dict]:
        """The user's own messages (both sides) in a time range, oldest first ("what did we talk about yesterday")."""
        user = self._auth.require(session)
        return self._db.query(
            "SELECT m.id, m.conversation_id, m.role, m.content, m.created_at FROM messages m "
            "WHERE m.user_id = ? AND m.created_at >= ? AND m.created_at < ? AND m.content != '' "
            "ORDER BY m.id LIMIT ?",
            (user.id, start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"), int(limit)))

    def search_messages(self, session: Session, text: str, limit: int = 40,
                        exclude_conversation: int | None = None) -> list[dict]:
        """Search the user's own messages (FTS5 ranked by relevance, newest first on ties)."""
        user = self._auth.require(session)
        exclude = int(exclude_conversation) if exclude_conversation is not None else -1
        query = fts_query(text)
        if query is None:
            return []
        if self._db.fts_enabled:
            return self._db.query(
                "SELECT m.id, m.conversation_id, m.role, m.content, m.created_at, m.status, c.title, "
                "bm25(messages_fts) AS rank FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
                "JOIN conversations c ON c.id = m.conversation_id "
                "WHERE messages_fts MATCH ? AND m.user_id = ? AND m.conversation_id != ? "
                "ORDER BY rank, m.id DESC LIMIT ?", (query, user.id, exclude, int(limit)))
        like = [f"%{k}%" for k in keywords(text)]
        clause = " OR ".join("m.content LIKE ?" for _ in like)
        return self._db.query(
            f"SELECT m.id, m.conversation_id, m.role, m.content, m.created_at, m.status, c.title, 0 AS rank "
            f"FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            f"WHERE m.user_id = ? AND m.conversation_id != ? AND ({clause}) ORDER BY m.id DESC LIMIT ?",
            (user.id, exclude, *like, int(limit)))

    # -- managing -------------------------------------------------------------------------------
    def rename_conversation(self, session: Session, conversation_id: int, title: str) -> None:
        user = self._auth.require(session)
        title = " ".join((title or "").split())
        if not 1 <= len(title) <= 80:
            raise ValueError("Title must be 1-80 characters.")
        if not self._db.execute("UPDATE conversations SET title = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                                (title, self._now(), int(conversation_id), user.id)):
            raise HistoryNotFound()

    def set_archived(self, session: Session, conversation_id: int, archived: bool) -> None:
        user = self._auth.require(session)
        if not self._db.execute("UPDATE conversations SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                                ("archived" if archived else "active", self._now(), int(conversation_id), user.id)):
            raise HistoryNotFound()

    def delete_conversation(self, session: Session, conversation_id: int) -> None:
        user = self._auth.require(session)
        if not self._db.execute("DELETE FROM conversations WHERE id = ? AND user_id = ?",
                                (int(conversation_id), user.id)):
            raise HistoryNotFound()

    def clear_history(self, session: Session, confirmed: bool = False) -> int:
        """Delete ALL of the user's conversations. Requires ``confirmed=True`` (the UI asks first)."""
        if not confirmed:
            raise ValueError("Clearing history must be confirmed.")
        user = self._auth.require(session)
        return self._db.execute("DELETE FROM conversations WHERE user_id = ?", (user.id,))

    def export(self, session: Session) -> dict[str, Any]:
        """Everything the user owns, as plain data (no password hashes, no tokens)."""
        user = self._auth.require(session, "history.export")
        conversations = []
        for conv in self._db.query("SELECT id, title, status, created_at, updated_at FROM conversations "
                                   "WHERE user_id = ? ORDER BY id", (user.id,)):
            conv["messages"] = self._db.query(
                "SELECT id, role, content, input_mode, status, provider, model, error_kind, reply_to, created_at "
                "FROM messages WHERE conversation_id = ? AND user_id = ? ORDER BY id", (conv["id"], user.id))
            conversations.append(conv)
        return {
            "exported_at": self._now(),
            "account": {"username": user.username, "display_name": user.display_name, "role": user.role},
            "preferences": self._db.query("SELECT preference_key, preference_value, updated_at FROM user_preferences "
                                          "WHERE user_id = ?", (user.id,)),
            "memories": self._db.query(
                "SELECT id, memory_key, memory_value, category, confidence, verification_status, sensitivity, status, "
                "created_at, updated_at FROM long_term_memory WHERE user_id = ? AND status != 'deleted' ORDER BY id",
                (user.id,)),
            "conversations": conversations,
            "tasks": self._db.query("SELECT title, notes, priority, due_date, completed, created_at FROM tasks "
                                    "WHERE user_id = ?", (user.id,)),
            "events": self._db.query("SELECT title, description, starts_at, reminder_minutes FROM events "
                                     "WHERE user_id = ?", (user.id,)),
            "automation_tasks": self._db.query(
                "SELECT request, title, source, status, steps_json, result_summary, error, workflow_name, created_at, "
                "finished_at FROM automation_tasks WHERE user_id = ? ORDER BY id", (user.id,)),
            "research_sessions": self._db.query(
                "SELECT query, kind, status, summary, sources_json, period, user_notes, created_at, finished_at "
                "FROM research_sessions WHERE user_id = ? ORDER BY id", (user.id,)),
        }

    def export_json(self, session: Session) -> str:
        return json.dumps(self.export(session), indent=2, ensure_ascii=False)

    def stats(self, session: Session) -> dict[str, int]:
        user = self._auth.require(session)
        return {
            "conversations": self._db.scalar("SELECT COUNT(*) FROM conversations WHERE user_id = ?", (user.id,)),
            "messages": self._db.scalar("SELECT COUNT(*) FROM messages WHERE user_id = ?", (user.id,)),
        }

    # -- retention ---------------------------------------------------------------------------------
    def purge_expired(self, retention_days: int) -> int:
        """System retention policy (0 = keep forever). Removes old conversations of every user."""
        if retention_days <= 0:
            return 0
        cutoff = (self._clock() - timedelta(days=retention_days)).isoformat(timespec="seconds")
        removed = self._db.execute("DELETE FROM conversations WHERE updated_at < ?", (cutoff,))
        if removed:
            log.info("Retention: removed %d old conversation(s)", removed)
        return removed
