"""SQLite database with versioned, idempotent migrations.

The schema version is stored in ``PRAGMA user_version``. Each entry in
``MIGRATIONS`` upgrades the database by exactly one version inside one
transaction, so a failed upgrade leaves the previous version untouched.
Before an existing database is upgraded, a full copy is written to
``data/backups/`` (see "Backup and restore" in the README).

Rules that every migration follows:

* never drop or rewrite existing user data; only add tables, columns, indexes;
* existing rows are adopted, never deleted (e.g. v2 adds ``user_id`` to tasks
  and events; old rows are given to the owner when the owner account is created).

One connection is shared behind a lock. Foreign keys are enforced
(``PRAGMA foreign_keys = ON``) and WAL journaling is used so reads and writes
from worker threads do not block each other for long. The dashboard never
calls this module directly; it goes through the services.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

log = logging.getLogger(__name__)


class DatabaseError(RuntimeError):
    pass


V1_TASKS_EVENTS = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    notes       TEXT    NOT NULL DEFAULT '',
    priority    TEXT    NOT NULL DEFAULT 'medium' CHECK (priority IN ('low','medium','high')),
    due_date    TEXT,
    completed   INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0,1)),
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_completed ON tasks(completed, due_date);

CREATE TABLE IF NOT EXISTS events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT    NOT NULL,
    description      TEXT    NOT NULL DEFAULT '',
    starts_at        TEXT    NOT NULL,
    reminder_minutes INTEGER CHECK (reminder_minutes IS NULL OR reminder_minutes >= 0),
    reminded_at      TEXT,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_starts ON events(starts_at);
"""

# v2: accounts, sessions, permissions, conversation history, long-term memory, audits.
V2_ACCOUNTS_HISTORY_MEMORY: list[str] = [
    """CREATE TABLE users (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        username             TEXT NOT NULL,
        username_norm        TEXT NOT NULL UNIQUE,
        password_hash        TEXT NOT NULL,
        display_name         TEXT NOT NULL,
        role                 TEXT NOT NULL CHECK (role IN ('owner','user')),
        status               TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
        must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0,1)),
        created_at           TEXT NOT NULL,
        updated_at           TEXT NOT NULL,
        last_login_at        TEXT,
        password_changed_at  TEXT
    )""",
    # Exactly one owner can exist; the database itself refuses a second one.
    "CREATE UNIQUE INDEX ux_users_single_owner ON users(role) WHERE role = 'owner'",

    """CREATE TABLE user_preferences (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        preference_key   TEXT NOT NULL,
        preference_value TEXT NOT NULL,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        UNIQUE (user_id, preference_key)
    )""",

    """CREATE TABLE user_permissions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        permission  TEXT NOT NULL,
        granted     INTEGER NOT NULL CHECK (granted IN (0,1)),
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        UNIQUE (user_id, permission)
    )""",

    # Only a SHA-256 hash of each session token is stored; the token itself never touches disk
    # (except DPAPI-encrypted for "stay signed in", see services/auth_service.py).
    """CREATE TABLE user_sessions (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token_hash    TEXT NOT NULL UNIQUE,
        persistent    INTEGER NOT NULL DEFAULT 0 CHECK (persistent IN (0,1)),
        created_at    TEXT NOT NULL,
        last_seen_at  TEXT NOT NULL,
        expires_at    TEXT NOT NULL,
        revoked_at    TEXT,
        revoke_reason TEXT
    )""",
    "CREATE INDEX idx_sessions_user ON user_sessions(user_id, revoked_at)",

    """CREATE TABLE login_attempts (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username_norm TEXT NOT NULL,
        success       INTEGER NOT NULL CHECK (success IN (0,1)),
        attempted_at  TEXT NOT NULL
    )""",
    "CREATE INDEX idx_login_attempts ON login_attempts(username_norm, attempted_at)",

    """CREATE TABLE admin_audit (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        actor_user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
        action         TEXT NOT NULL,
        target_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        timestamp      TEXT NOT NULL,
        metadata       TEXT NOT NULL DEFAULT '{}'
    )""",
    "CREATE INDEX idx_admin_audit_time ON admin_audit(timestamp)",

    """CREATE TABLE conversations (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title       TEXT NOT NULL DEFAULT 'New conversation',
        status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )""",
    "CREATE INDEX idx_conversations_user ON conversations(user_id, status, updated_at)",

    """CREATE TABLE messages (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        role            TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
        content         TEXT NOT NULL,
        input_mode      TEXT CHECK (input_mode IS NULL OR input_mode IN ('voice','text')),
        status          TEXT NOT NULL CHECK (status IN ('pending','completed','failed','interrupted')),
        provider        TEXT,
        model           TEXT,
        error_kind      TEXT,
        request_key     TEXT,
        reply_to        INTEGER REFERENCES messages(id) ON DELETE CASCADE,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )""",
    "CREATE UNIQUE INDEX ux_messages_request ON messages(user_id, request_key) WHERE request_key IS NOT NULL",
    "CREATE UNIQUE INDEX ux_messages_one_reply ON messages(reply_to) WHERE role = 'assistant'",
    "CREATE INDEX idx_messages_conversation ON messages(conversation_id, id)",
    "CREATE INDEX idx_messages_user_time ON messages(user_id, created_at)",
    # Defence in depth: a message can never be attached to another user's conversation.
    """CREATE TRIGGER trg_messages_owner_check BEFORE INSERT ON messages
       WHEN (SELECT user_id FROM conversations WHERE id = NEW.conversation_id) IS NOT NEW.user_id
       BEGIN SELECT RAISE(ABORT, 'message user does not own the conversation'); END""",

    """CREATE TABLE long_term_memory (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        memory_key          TEXT NOT NULL,
        key_norm            TEXT NOT NULL,
        memory_value        TEXT NOT NULL,
        category            TEXT NOT NULL DEFAULT 'general'
                            CHECK (category IN ('identity','preference','project','goal','fact','instruction','general')),
        source_message_id   INTEGER REFERENCES messages(id) ON DELETE SET NULL,
        confidence          REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
        verification_status TEXT NOT NULL DEFAULT 'user_confirmed'
                            CHECK (verification_status IN ('user_confirmed','user_stated','inferred')),
        sensitivity         TEXT NOT NULL DEFAULT 'normal' CHECK (sensitivity IN ('normal','sensitive')),
        status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','deleted')),
        supersedes_id       INTEGER REFERENCES long_term_memory(id) ON DELETE SET NULL,
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL,
        last_accessed_at    TEXT,
        expires_at          TEXT
    )""",
    # No two ACTIVE memories of one user may share a key: corrections supersede the old row.
    "CREATE UNIQUE INDEX ux_memory_active_key ON long_term_memory(user_id, key_norm) WHERE status = 'active'",
    "CREATE INDEX idx_memory_user ON long_term_memory(user_id, status, category, updated_at)",

    """CREATE TABLE memory_audit (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        memory_id  INTEGER REFERENCES long_term_memory(id) ON DELETE SET NULL,
        action     TEXT NOT NULL CHECK (action IN ('create','update','supersede','delete','clear','confirm','access')),
        timestamp  TEXT NOT NULL,
        metadata   TEXT NOT NULL DEFAULT '{}'
    )""",
    "CREATE INDEX idx_memory_audit_user ON memory_audit(user_id, timestamp)",

    # Tasks and schedule become private per user. Existing rows keep user_id NULL
    # until the owner account is created, which adopts them (nothing is deleted).
    "ALTER TABLE tasks ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE",
    "ALTER TABLE events ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE",
    "CREATE INDEX idx_tasks_user ON tasks(user_id, completed, due_date)",
    "CREATE INDEX idx_events_user ON events(user_id, starts_at)",
]

# Full-text search (FTS5) for messages and memories. Applied with v2 when the SQLite
# build supports FTS5 (Python 3.11 on Windows does); otherwise search falls back to LIKE.
V2_FTS: list[str] = [
    """CREATE VIRTUAL TABLE messages_fts USING fts5(
        content, content='messages', content_rowid='id', tokenize='porter unicode61')""",
    """CREATE TRIGGER trg_messages_fts_ai AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content); END""",
    """CREATE TRIGGER trg_messages_fts_ad AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content); END""",
    """CREATE TRIGGER trg_messages_fts_au AFTER UPDATE OF content ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
        INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content); END""",
    """CREATE VIRTUAL TABLE memory_fts USING fts5(
        memory_key, memory_value, category, content='long_term_memory', content_rowid='id',
        tokenize='porter unicode61')""",
    """CREATE TRIGGER trg_memory_fts_ai AFTER INSERT ON long_term_memory BEGIN
        INSERT INTO memory_fts(rowid, memory_key, memory_value, category)
        VALUES (new.id, new.memory_key, new.memory_value, new.category); END""",
    """CREATE TRIGGER trg_memory_fts_ad AFTER DELETE ON long_term_memory BEGIN
        INSERT INTO memory_fts(memory_fts, rowid, memory_key, memory_value, category)
        VALUES ('delete', old.id, old.memory_key, old.memory_value, old.category); END""",
    """CREATE TRIGGER trg_memory_fts_au AFTER UPDATE OF memory_key, memory_value, category ON long_term_memory BEGIN
        INSERT INTO memory_fts(memory_fts, rowid, memory_key, memory_value, category)
        VALUES ('delete', old.id, old.memory_key, old.memory_value, old.category);
        INSERT INTO memory_fts(rowid, memory_key, memory_value, category)
        VALUES (new.id, new.memory_key, new.memory_value, new.category); END""",
    "INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')",
    "INSERT INTO memory_fts(memory_fts) VALUES ('rebuild')",
]

# v3: laptop automation task history (Module 4). Steps are stored as JSON with their
# status, verification and a *safe* copy of their arguments (typed text, form values
# and secrets are never stored). Workflows the user asked DUMBOO to remember are
# tasks with a workflow name.
V3_AUTOMATION: list[str] = [
    """CREATE TABLE automation_tasks (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        request        TEXT NOT NULL,
        title          TEXT NOT NULL DEFAULT '',
        source         TEXT NOT NULL DEFAULT 'typed',
        origin         TEXT NOT NULL DEFAULT 'rules',
        status         TEXT NOT NULL CHECK (status IN ('pending','awaiting_confirmation','running','completed',
                                                       'partially_completed','failed','cancelled')),
        confirmation   TEXT NOT NULL DEFAULT 'not_required'
                       CHECK (confirmation IN ('not_required','requested','approved','denied','expired')),
        steps_json     TEXT NOT NULL DEFAULT '[]',
        result_summary TEXT NOT NULL DEFAULT '',
        error          TEXT NOT NULL DEFAULT '',
        undo_json      TEXT NOT NULL DEFAULT '[]',
        workflow_name  TEXT,
        created_at     TEXT NOT NULL,
        started_at     TEXT,
        finished_at    TEXT,
        updated_at     TEXT NOT NULL
    )""",
    "CREATE INDEX idx_automation_user ON automation_tasks(user_id, created_at)",
    "CREATE UNIQUE INDEX ux_automation_workflow ON automation_tasks(user_id, workflow_name) "
    "WHERE workflow_name IS NOT NULL",
]

# v4: research sessions (Module 5). Sources are stored as metadata plus a short extract (never whole
# articles); the answer text is DUMBOO's own summary. ``refreshed_from`` links a refreshed run to the
# original, ``memory_id`` to findings the user asked DUMBOO to remember.
V4_RESEARCH: list[str] = [
    """CREATE TABLE research_sessions (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        query          TEXT NOT NULL,
        kind           TEXT NOT NULL CHECK (kind IN ('search','news','research','factcheck','open_docs')),
        status         TEXT NOT NULL CHECK (status IN ('running','completed','partial','failed','cancelled')),
        source         TEXT NOT NULL DEFAULT 'typed',
        summary        TEXT NOT NULL DEFAULT '',
        answer         TEXT NOT NULL DEFAULT '',
        findings_json  TEXT NOT NULL DEFAULT '[]',
        sources_json   TEXT NOT NULL DEFAULT '[]',
        plan_json      TEXT NOT NULL DEFAULT '[]',
        notes_json     TEXT NOT NULL DEFAULT '[]',
        period         TEXT NOT NULL DEFAULT '',
        error          TEXT NOT NULL DEFAULT '',
        user_notes     TEXT NOT NULL DEFAULT '',
        refreshed_from INTEGER REFERENCES research_sessions(id) ON DELETE SET NULL,
        memory_id      INTEGER REFERENCES long_term_memory(id) ON DELETE SET NULL,
        created_at     TEXT NOT NULL,
        finished_at    TEXT,
        updated_at     TEXT NOT NULL
    )""",
    "CREATE INDEX idx_research_user ON research_sessions(user_id, created_at)",
]

# v5: face unlock. Only face *measurements* (SFace feature vectors) are stored, encrypted with
# Windows DPAPI for the current Windows user, never images. ``face_attempts`` drives the lockout
# (too many failed face sign-ins pause face unlock until a password sign-in).
V5_FACE: list[str] = [
    """CREATE TABLE face_profiles (
        user_id     INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        embeddings  BLOB NOT NULL,
        samples     INTEGER NOT NULL,
        model       TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )""",
    """CREATE TABLE face_attempts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER REFERENCES users(id) ON DELETE CASCADE,
        success      INTEGER NOT NULL,
        reason       TEXT NOT NULL DEFAULT '',
        attempted_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_face_attempts_time ON face_attempts(attempted_at)",
]

# v6: Security Guardian (Module 7). Scan results and findings keep evidence summaries, never
# passwords or full secret-bearing command lines. Integrity baselines hold hashes of the files the
# user chose to watch. ``security_actions`` is the audit log of every proposed/confirmed action.
V6_SECURITY: list[str] = [
    """CREATE TABLE security_scans (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind         TEXT NOT NULL,
        status       TEXT NOT NULL CHECK (status IN ('running','completed','partial','failed','cancelled')),
        summary      TEXT NOT NULL DEFAULT '',
        result_json  TEXT NOT NULL DEFAULT '{}',
        error        TEXT NOT NULL DEFAULT '',
        started_at   TEXT NOT NULL,
        finished_at  TEXT
    )""",
    "CREATE INDEX idx_security_scans_user ON security_scans(user_id, started_at)",
    """CREATE TABLE security_findings (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_id      INTEGER NOT NULL REFERENCES security_scans(id) ON DELETE CASCADE,
        user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        category     TEXT NOT NULL,
        subject      TEXT NOT NULL,
        status       TEXT NOT NULL,
        assessment   TEXT NOT NULL DEFAULT '',
        confidence   TEXT NOT NULL DEFAULT '',
        evidence_json TEXT NOT NULL DEFAULT '{}',
        source       TEXT NOT NULL DEFAULT '',
        limitations  TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL
    )""",
    "CREATE INDEX idx_security_findings_user ON security_findings(user_id, created_at)",
    """CREATE TABLE integrity_scopes (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        path         TEXT NOT NULL,
        label        TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL,
        baseline_at  TEXT,
        checked_at   TEXT,
        UNIQUE (user_id, path)
    )""",
    """CREATE TABLE integrity_baselines (
        scope_id     INTEGER NOT NULL REFERENCES integrity_scopes(id) ON DELETE CASCADE,
        rel_path     TEXT NOT NULL,
        sha256       TEXT NOT NULL,
        size         INTEGER NOT NULL,
        mtime        TEXT NOT NULL,
        PRIMARY KEY (scope_id, rel_path)
    )""",
    """CREATE TABLE integrity_events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        scope_id     INTEGER NOT NULL REFERENCES integrity_scopes(id) ON DELETE CASCADE,
        user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind         TEXT NOT NULL CHECK (kind IN ('modified','missing','new')),
        rel_path     TEXT NOT NULL,
        old_sha256   TEXT,
        new_sha256   TEXT,
        detected_at  TEXT NOT NULL
    )""",
    "CREATE INDEX idx_integrity_events_user ON integrity_events(user_id, detected_at)",
    """CREATE TABLE security_actions (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        action       TEXT NOT NULL,
        level        INTEGER NOT NULL,
        target       TEXT NOT NULL,
        status       TEXT NOT NULL CHECK (status IN ('proposed','confirmed','declined','completed','failed',
                                                    'not_supported','restored')),
        details_json TEXT NOT NULL DEFAULT '{}',
        recovery_json TEXT NOT NULL DEFAULT '{}',
        created_at   TEXT NOT NULL,
        finished_at  TEXT
    )""",
    "CREATE INDEX idx_security_actions_user ON security_actions(user_id, created_at)",
]

# v7: tasks get an optional due time and their own reminder (voice: "add a task to pay rent by Friday 5 pm").
# Columns are only added; existing tasks keep working with all three empty.
V7_TASK_REMINDERS: list[str] = [
    "ALTER TABLE tasks ADD COLUMN due_time TEXT",
    "ALTER TABLE tasks ADD COLUMN remind_at TEXT",
    "ALTER TABLE tasks ADD COLUMN reminded_at TEXT",
    "CREATE INDEX idx_tasks_remind ON tasks(user_id, remind_at)",
]

MIGRATIONS: list[str | list[str]] = [V1_TASKS_EVENTS, V2_ACCOUNTS_HISTORY_MEMORY, V3_AUTOMATION, V4_RESEARCH,
                                     V5_FACE, V6_SECURITY, V7_TASK_REMINDERS]
OPTIONAL_FTS = {2: V2_FTS}  # version -> statements applied only if FTS5 is available

SCHEMA_VERSION = len(MIGRATIONS)


def _statements(migration: str | list[str]) -> list[str]:
    if isinstance(migration, list):
        return migration
    return [s for s in migration.split(";") if s.strip()]


def fts5_supported() -> bool:
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        conn.close()
        return True
    except sqlite3.Error:
        return False


class LocalDatabase:
    name = "database"

    def __init__(self, path: Path | str, backup_dir: Path | None = None) -> None:
        self.path = str(path)
        in_memory = self.path == ":memory:"
        if not in_memory:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir = backup_dir or (None if in_memory else Path(self.path).parent / "backups")
        self._lock = threading.RLock()
        self.last_backup: Path | None = None
        try:
            self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=10)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            if not in_memory:
                self._conn.execute("PRAGMA journal_mode = WAL")
            self.migrate()
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Could not open or upgrade the database ({exc}). Nothing was deleted. "
                f"See README 'Backup and restore' to recover from data/backups/."
            ) from exc

    # -- schema --------------------------------------------------------------------
    @property
    def version(self) -> int:
        with self._lock:
            return self._conn.execute("PRAGMA user_version").fetchone()[0]

    @property
    def fts_enabled(self) -> bool:
        return self.scalar("SELECT COUNT(*) FROM sqlite_master WHERE name = 'messages_fts'") == 1

    def migrate(self) -> None:
        with self._lock:
            current = self.version
            if current > SCHEMA_VERSION:
                raise sqlite3.DatabaseError(
                    f"database schema v{current} is newer than this DUMBOO (v{SCHEMA_VERSION}); update DUMBOO")
            if current == SCHEMA_VERSION:
                return
            if current > 0:
                self.last_backup = self.backup(reason=f"before-v{current}-to-v{SCHEMA_VERSION}")
            fts = fts5_supported()
            for index in range(current, SCHEMA_VERSION):
                version = index + 1
                log.info("Applying database migration v%d", version)
                statements = _statements(MIGRATIONS[index])
                if fts and version in OPTIONAL_FTS:
                    statements = statements + OPTIONAL_FTS[version]
                elif version in OPTIONAL_FTS:
                    log.warning("SQLite FTS5 not available: search will use slower LIKE matching")
                self._conn.execute("BEGIN")
                try:
                    for statement in statements:
                        self._conn.execute(statement)
                    self._conn.execute(f"PRAGMA user_version = {version}")
                    self._conn.execute("COMMIT")
                except sqlite3.Error:
                    self._conn.execute("ROLLBACK")
                    log.exception("Migration v%d failed; database left at v%d", version, index)
                    raise
            fk_problems = self._conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk_problems:
                log.warning("Foreign-key check found %d problem rows after migration", len(fk_problems))

    # -- backups ---------------------------------------------------------------------
    def backup(self, destination: Path | None = None, reason: str = "manual") -> Path:
        """Write a consistent copy of the whole database (safe while DUMBOO runs)."""
        if destination is None:
            if self.backup_dir is None:
                raise DatabaseError("No backup folder for an in-memory database")
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            destination = self.backup_dir / f"dumboo-{stamp}-{reason}.db"
        with self._lock:
            target = sqlite3.connect(str(destination))
            try:
                self._conn.backup(target)
            finally:
                target.close()
        log.info("Database backup written: %s", destination.name)
        return destination

    def integrity_check(self) -> str:
        return str(self.scalar("PRAGMA quick_check"))

    # -- queries ------------------------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """All statements inside run atomically: either all are saved or none."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def execute(self, sql: str, params: Sequence[Any] | dict = ()) -> int:
        """Run one write statement; returns lastrowid (inserts) or rowcount."""
        try:
            with self.transaction() as conn:
                cur = conn.execute(sql, params)
                return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount
        except sqlite3.IntegrityError:
            raise
        except sqlite3.Error as exc:
            log.error("Database write failed: %s", exc.__class__.__name__)
            raise DatabaseError("The database could not save that change. Please try again.") from exc

    def query(self, sql: str, params: Sequence[Any] | dict = ()) -> list[dict[str, Any]]:
        try:
            with self._lock:
                return [dict(row) for row in self._conn.execute(sql, params).fetchall()]
        except sqlite3.Error as exc:
            log.error("Database read failed: %s", exc.__class__.__name__)
            raise DatabaseError("The database could not be read. Please try again.") from exc

    def row(self, sql: str, params: Sequence[Any] | dict = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] | dict = ()) -> Any:
        r = self.row(sql, params)
        return next(iter(r.values())) if r else None

    # -- status ---------------------------------------------------------------------------
    def size_bytes(self) -> int:
        if self.path == ":memory:":
            return 0
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(self.path + suffix)
            if p.exists():
                total += p.stat().st_size
        return total

    def start(self) -> None:  # lifecycle hook; opening happens in __init__
        pass

    def stop(self) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
