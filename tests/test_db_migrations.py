"""Migrations preserve data, foreign keys hold, failures are safe."""

import sqlite3

import pytest

import storage.local_database as dbmod
from security.passwords import make_hasher
from services.auth_service import AuthService
from storage.local_database import DatabaseError, LocalDatabase
from tests.conftest import POLICY


def make_v1_database(path):
    """A database exactly as Module 1/2 left it (schema v1) with real rows in it."""
    conn = sqlite3.connect(path)
    conn.executescript(dbmod.V1_TASKS_EVENTS)
    conn.execute("INSERT INTO tasks (title, created_at, updated_at) VALUES ('Old task', 'now', 'now')")
    conn.execute("INSERT INTO events (title, starts_at, created_at, updated_at) VALUES "
                 "('Old event', '2026-10-30T10:00', 'now', 'now')")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()


def test_upgrade_from_v1_keeps_every_row_and_makes_a_backup(tmp_path):
    path = tmp_path / "dumboo.db"
    make_v1_database(path)
    db = LocalDatabase(path)
    assert db.version == dbmod.SCHEMA_VERSION
    assert db.query("SELECT title, user_id FROM tasks") == [{"title": "Old task", "user_id": None}]
    assert db.scalar("SELECT title FROM events") == "Old event"
    assert db.last_backup is not None and db.last_backup.exists()
    backup = sqlite3.connect(db.last_backup)
    assert backup.execute("PRAGMA user_version").fetchone()[0] == 1          # the backup is the old version
    assert backup.execute("SELECT title FROM tasks").fetchone()[0] == "Old task"
    backup.close()
    # the owner adopts the old rows at first-run setup
    auth = AuthService(db, lambda: dict(POLICY), hasher=make_hasher(fast=True))
    owner_id = auth.setup_owner("alice", "Alice", "Owner-Pass-123")
    assert db.scalar("SELECT user_id FROM tasks") == owner_id
    assert db.scalar("SELECT user_id FROM events") == owner_id
    db.close()


def test_reopening_does_not_migrate_or_back_up_again(tmp_path):
    path = tmp_path / "dumboo.db"
    LocalDatabase(path).close()
    db = LocalDatabase(path)
    assert db.last_backup is None and db.version == dbmod.SCHEMA_VERSION
    db.close()


def test_failed_migration_rolls_back_and_keeps_data(tmp_path, monkeypatch):
    path = tmp_path / "dumboo.db"
    make_v1_database(path)
    monkeypatch.setattr(dbmod, "MIGRATIONS", [dbmod.V1_TASKS_EVENTS, ["CREATE TABLE ok_table (x INTEGER)",
                                                                      "THIS IS NOT SQL"]])
    monkeypatch.setattr(dbmod, "SCHEMA_VERSION", 2)
    with pytest.raises(DatabaseError) as info:
        LocalDatabase(path)
    assert "Nothing was deleted" in str(info.value)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert conn.execute("SELECT title FROM tasks").fetchone()[0] == "Old task"
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'ok_table'").fetchone()[0] == 0
    conn.close()


def test_newer_schema_is_refused_not_damaged(tmp_path):
    path = tmp_path / "dumboo.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(DatabaseError):
        LocalDatabase(path)


def test_foreign_keys_are_enforced(accounts):
    db = accounts.db
    assert db.scalar("PRAGMA foreign_keys") == 1
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO conversations (user_id, title, created_at, updated_at) VALUES (9999, 't', 'n', 'n')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO user_preferences (user_id, preference_key, preference_value, created_at, updated_at) "
                   "VALUES (9999, 'k', 'v', 'n', 'n')")
    with pytest.raises(sqlite3.IntegrityError):  # check constraints too
        db.execute("UPDATE users SET role = 'superuser' WHERE id = ?", (accounts.user_id,))


def test_one_active_memory_per_key_is_enforced_by_the_database(accounts):
    insert = ("INSERT INTO long_term_memory (user_id, memory_key, key_norm, memory_value, created_at, updated_at) "
              "VALUES (?, 'project', 'project', ?, 'n', 'n')")
    accounts.db.execute(insert, (accounts.owner_id, "A"))
    with pytest.raises(sqlite3.IntegrityError):
        accounts.db.execute(insert, (accounts.owner_id, "B"))
    accounts.db.execute(insert, (accounts.user_id, "C"))  # another user may use the same key


def test_database_errors_are_reported_safely(tmp_path):
    db = LocalDatabase(tmp_path / "x.db")
    db.close()
    with pytest.raises(DatabaseError) as info:
        db.query("SELECT * FROM users WHERE username_norm = 'alice'")
    assert "alice" not in str(info.value) and "SELECT" not in str(info.value)


def test_backup_and_integrity_check(accounts, tmp_path):
    target = accounts.db.backup(tmp_path / "copy.db")
    copy = LocalDatabase(target)
    assert copy.scalar("SELECT COUNT(*) FROM users") == 2
    assert accounts.db.integrity_check() == "ok"
    copy.close()


def test_fts_search_stays_in_sync_with_edits(accounts):
    from services.memory_service import MemoryService

    memory = MemoryService(accounts.db, accounts.auth)
    m = memory.remember(accounts.owner, "favourite fruit", "mango").memory
    assert memory.search(accounts.owner, "mango")
    memory.update(accounts.owner, m["id"], value="papaya")
    assert not memory.search(accounts.owner, "mango") and memory.search(accounts.owner, "papaya")
    memory.forget(accounts.owner, m["id"])
    assert not memory.search(accounts.owner, "papaya")
