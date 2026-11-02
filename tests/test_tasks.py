from datetime import date, datetime

import pytest

from services.auth_service import NotAuthenticated, PermissionDenied
from services.task_service import TaskService, ValidationError
from storage.local_database import SCHEMA_VERSION, LocalDatabase


@pytest.fixture
def tasks(accounts):
    return TaskService(accounts.db, accounts.auth)


def test_schema_is_versioned_and_idempotent(paths):
    db = LocalDatabase(paths.database)
    assert db.version == SCHEMA_VERSION
    db.migrate()  # running again is harmless
    db.close()
    again = LocalDatabase(paths.database)
    assert again.version == SCHEMA_VERSION
    again.close()


def test_create_read_update_delete(tasks, accounts):
    s = accounts.owner
    t = tasks.create(s, "  Buy   milk ", priority="high", due_date="2026-10-25", notes="2 litres")
    assert t.id > 0 and t.title == "Buy milk" and t.priority == "high"
    assert t.due_date == date(2026, 10, 25) and not t.completed

    t = tasks.update(s, t.id, title="Buy oat milk", priority="low", due_date=None)
    assert (t.title, t.priority, t.due_date) == ("Buy oat milk", "low", None)
    assert t.notes == "2 litres"  # untouched fields are kept

    t = tasks.set_completed(s, t.id, True)
    assert t.completed and t.completed_at is not None
    t = tasks.set_completed(s, t.id, False)
    assert not t.completed and t.completed_at is None

    tasks.delete(s, t.id)
    with pytest.raises(KeyError):
        tasks.get(s, t.id)


def test_pending_and_completed_lists(tasks, accounts):
    s = accounts.owner
    a = tasks.create(s, "a", priority="low")
    b = tasks.create(s, "b", priority="high")
    c = tasks.create(s, "c", due_date=date(2026, 1, 1))
    tasks.set_completed(s, a.id, True)
    assert [t.id for t in tasks.list(s, completed=False)] == [c.id, b.id]  # dated first, then by priority
    assert [t.id for t in tasks.list(s, completed=True)] == [a.id]
    assert tasks.counts(s) == {"pending": 2, "completed": 1}


@pytest.mark.parametrize("kwargs", [
    {"title": ""}, {"title": "   "}, {"title": "x" * 201}, {"title": "ok", "priority": "urgent"},
    {"title": "ok", "due_date": "25/10/2026"}, {"title": "ok", "due_date": "2026-02-30"},
    {"title": "ok", "notes": "n" * 2001},
])
def test_validation(tasks, accounts, kwargs):
    with pytest.raises(ValidationError):
        tasks.create(accounts.owner, **kwargs)


def test_sql_injection_is_stored_as_text(tasks, accounts):
    evil = "x'); DROP TABLE tasks; --"
    t = tasks.create(accounts.owner, evil)
    assert tasks.get(accounts.owner, t.id).title == evil
    assert len(tasks.list(accounts.owner)) == 1


def test_missing_task_errors(tasks, accounts):
    s = accounts.owner
    with pytest.raises(KeyError):
        tasks.delete(s, 999)
    with pytest.raises(KeyError):
        tasks.set_completed(s, 999, True)
    with pytest.raises(KeyError):
        tasks.update(s, 999, title="x")


def test_overdue(tasks, accounts):
    t = tasks.create(accounts.owner, "old", due_date="2020-01-01")
    assert t.is_overdue(date(2026, 1, 1))
    assert not t.is_overdue(date(2019, 1, 1))


def test_tasks_persist_after_restart(paths):
    from tests.conftest import Accounts

    env = Accounts(paths.database)
    TaskService(env.db, env.auth).create(env.owner, "survive restart")
    env.db.close()
    db2 = LocalDatabase(paths.database)
    from services.auth_service import AuthService
    auth2 = AuthService(db2, lambda: env.policy, hasher=env.auth._hasher)
    session = auth2.login("alice", "Owner-Pass-123")
    assert [t.title for t in TaskService(db2, auth2).list(session)] == ["survive restart"]
    db2.close()


def test_timestamps_use_clock(accounts):
    fixed = datetime(2026, 10, 21, 9, 30)
    t = TaskService(accounts.db, accounts.auth, clock=lambda: fixed).create(accounts.owner, "x")
    assert t.created_at == fixed


def test_tasks_are_private_per_user(tasks, accounts):
    mine = tasks.create(accounts.owner, "owner's secret task")
    tasks.create(accounts.user, "bob's task")
    assert [t.title for t in tasks.list(accounts.user)] == ["bob's task"]
    for action in (lambda: tasks.get(accounts.user, mine.id), lambda: tasks.delete(accounts.user, mine.id),
                   lambda: tasks.set_completed(accounts.user, mine.id, True),
                   lambda: tasks.update(accounts.user, mine.id, title="hacked")):
        with pytest.raises(KeyError):
            action()
    assert tasks.get(accounts.owner, mine.id).title == "owner's secret task"


def test_tasks_need_a_valid_session_and_permission(tasks, accounts):
    with pytest.raises(NotAuthenticated):
        tasks.list(None)
    accounts.auth.set_permission(accounts.owner, accounts.user_id, "tasks.manage", False)
    with pytest.raises(PermissionDenied):
        tasks.list(accounts.user)
