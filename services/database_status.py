"""Safe database health information for the dashboard.

Shows counts, sizes and checks only. Never returns password hashes, session
tokens, API keys or the content of anyone's messages or memories. The owner
sees totals across all accounts (counts only); a normal user sees their own counts.
"""

from __future__ import annotations

from services.auth_service import AuthService, Session
from storage.local_database import SCHEMA_VERSION, LocalDatabase


def database_status(db: LocalDatabase, auth: AuthService, session: Session, integrity: bool = False) -> dict:
    user = auth.require(session)
    status: dict = {
        "available": True,
        "schema_version": f"v{db.version} (this DUMBOO expects v{SCHEMA_VERSION})",
        "full_text_search": "enabled (FTS5)" if db.fts_enabled else "fallback (LIKE)",
        "file_size_mb": round(db.size_bytes() / 1024 / 1024, 2),
        "location": db.path if user.is_owner else "(owner only)",
        "last_backup": db.last_backup.name if db.last_backup else "none this session",
    }
    if user.is_owner:
        status["counts (all users)"] = {
            "users": db.scalar("SELECT COUNT(*) FROM users"),
            "active sessions": db.scalar("SELECT COUNT(*) FROM user_sessions WHERE revoked_at IS NULL"),
            "conversations": db.scalar("SELECT COUNT(*) FROM conversations"),
            "messages": db.scalar("SELECT COUNT(*) FROM messages"),
            "memories (active)": db.scalar("SELECT COUNT(*) FROM long_term_memory WHERE status = 'active'"),
            "tasks": db.scalar("SELECT COUNT(*) FROM tasks"),
            "events": db.scalar("SELECT COUNT(*) FROM events"),
        }
    status["your data"] = {
        "conversations": db.scalar("SELECT COUNT(*) FROM conversations WHERE user_id = ?", (user.id,)),
        "messages": db.scalar("SELECT COUNT(*) FROM messages WHERE user_id = ?", (user.id,)),
        "memories (active)": db.scalar("SELECT COUNT(*) FROM long_term_memory WHERE user_id = ? AND status = 'active'",
                                       (user.id,)),
        "tasks": db.scalar("SELECT COUNT(*) FROM tasks WHERE user_id = ?", (user.id,)),
        "events": db.scalar("SELECT COUNT(*) FROM events WHERE user_id = ?", (user.id,)),
    }
    if integrity:
        status["integrity_check"] = db.integrity_check()
    return status
