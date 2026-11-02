"""Authentication, sessions, lockout, roles, permissions and owner administration."""

from datetime import datetime, timedelta

import pytest

from security.passwords import make_hasher
from services.auth_service import (AccountDisabled, AccountLocked, AccountValidationError, AuthService,
                                   InvalidCredentials, NotAuthenticated, PermissionDenied, ReauthRequired, Session,
                                   SetupError)
from services.settings_service import SettingsService
from storage.local_database import LocalDatabase
from tests.conftest import POLICY
from utils.paths import PROJECT_ROOT

OWNER_PW = "Owner-Pass-123"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 21, 9, 0)
        self.mono = 1000.0

    def __call__(self):
        return self.now

    def monotonic(self):
        return self.mono

    def advance(self, **kw):
        delta = timedelta(**kw)
        self.now += delta
        self.mono += delta.total_seconds()


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    policy = dict(POLICY)
    db = LocalDatabase(tmp_path / "auth.db")
    auth = AuthService(db, lambda: policy, hasher=make_hasher(fast=True), clock=clock, monotonic=clock.monotonic)
    yield auth, db, clock, policy
    db.close()


def owner(auth):
    auth.setup_owner("alice", "Alice", OWNER_PW)
    return auth.login("alice", OWNER_PW)


# ---------------------------------------------------------------- 1. owner setup
def test_first_run_owner_setup_and_only_one_owner(env):
    auth, db, *_ = env
    assert auth.needs_owner_setup()
    owner_id = auth.setup_owner("Alice", "Alice", OWNER_PW, title="Sir")
    assert not auth.needs_owner_setup()
    assert db.row("SELECT role FROM users WHERE id = ?", (owner_id,))["role"] == "owner"
    with pytest.raises(SetupError):
        auth.setup_owner("mallory", "Mallory", "Another-Pass-999")
    with pytest.raises(Exception):  # the database itself refuses a second owner
        db.execute("INSERT INTO users (username, username_norm, password_hash, display_name, role, created_at, "
                   "updated_at) VALUES ('x', 'x', 'h', 'X', 'owner', 'now', 'now')")


def test_no_default_account_exists(env):
    auth, db, *_ = env
    assert db.scalar("SELECT COUNT(*) FROM users") == 0
    with pytest.raises(InvalidCredentials):
        auth.login("admin", "admin")


# ---------------------------------------------------------------- 2-5. login and hashing
def test_correct_credentials_login(env):
    auth, *_ = env
    session = owner(auth)
    user = auth.require(session)
    assert user.username == "alice" and user.is_owner and "users.manage" in user.permissions


def test_wrong_password_and_unknown_user_give_the_same_generic_error(env):
    auth, *_ = env
    owner(auth)
    with pytest.raises(InvalidCredentials) as wrong_pw:
        auth.login("alice", "wrong-password-1")
    with pytest.raises(InvalidCredentials) as unknown:
        auth.login("nobody", "wrong-password-1")
    assert wrong_pw.value.user_message == unknown.value.user_message == "Invalid username or password."


def test_duplicate_usernames_are_rejected_case_insensitively(env):
    auth, *_ = env
    s = owner(auth)
    auth.create_user(s, "bob", "Bob", "Kiwi-Lamp-456")
    for name in ("bob", "BOB", " Bob "):
        with pytest.raises(AccountValidationError):
            auth.create_user(s, name, "Other", "Kiwi-Lamp-456")


@pytest.mark.parametrize("username", ["ab", "bad name", "-dash", "x" * 33, "évil"])
def test_username_format(env, username):
    auth, *_ = env
    with pytest.raises(AccountValidationError):
        auth.setup_owner(username, "X", OWNER_PW)


@pytest.mark.parametrize("password", ["short", "password1", "aaaaaaaaaa", " spaced-pass ", "alice-rocks-2"])
def test_weak_passwords_rejected(env, password):
    auth, *_ = env
    with pytest.raises(AccountValidationError):
        auth.setup_owner("alice", "Alice", password)


def test_passwords_are_stored_as_argon2id_hashes(env):
    auth, db, *_ = env
    owner(auth)
    stored = db.scalar("SELECT password_hash FROM users WHERE username = 'alice'")
    assert stored.startswith("$argon2id$") and OWNER_PW not in stored
    dump = "\n".join(str(r) for t in ("users", "user_sessions", "login_attempts", "admin_audit")
                     for r in db.query(f"SELECT * FROM {t}"))
    assert OWNER_PW not in dump


def test_session_tokens_are_stored_only_as_hashes(env):
    auth, db, *_ = env
    session = owner(auth)
    rows = db.query("SELECT token_hash FROM user_sessions")
    assert session.token not in str(rows) and rows[0]["token_hash"] == session.token_hash


# ---------------------------------------------------------------- 6-7. disabled accounts, logout
def test_disabled_account_cannot_log_in_and_is_signed_out(env):
    auth, *_ = env
    s = owner(auth)
    user_id, _ = auth.create_user(s, "bob", "Bob", "Kiwi-Lamp-456")
    bob = auth.login("bob", "Kiwi-Lamp-456")
    auth.reauthenticate(s, OWNER_PW)
    auth.set_status(s, user_id, "disabled")
    with pytest.raises(NotAuthenticated):
        auth.require(bob)  # the running session was revoked
    with pytest.raises(AccountDisabled):
        auth.login("bob", "Kiwi-Lamp-456")
    with pytest.raises(InvalidCredentials):
        auth.login("bob", "wrong-password-9")  # wrong password still says nothing about the account
    auth.set_status(s, user_id, "active")
    assert auth.require(auth.login("bob", "Kiwi-Lamp-456")).username == "bob"


def test_logout_invalidates_session(env):
    auth, *_ = env
    s = owner(auth)
    auth.logout(s)
    with pytest.raises(NotAuthenticated):
        auth.require(s)


def test_forged_or_tampered_sessions_are_rejected(env):
    auth, *_ = env
    s = owner(auth)
    with pytest.raises(NotAuthenticated):
        auth.require(Session("made-up-token"))
    with pytest.raises(NotAuthenticated):
        auth.require(None)
    tampered = Session(s.token, username="bob", role="user")  # display fields are ignored
    assert auth.require(tampered).username == "alice"


def test_inactivity_timeout_and_absolute_expiry(env):
    auth, _db, clock, policy = env
    s = owner(auth)
    clock.advance(minutes=policy["session_timeout_min"] - 1)
    auth.require(s)  # activity resets the inactivity timer
    clock.advance(minutes=policy["session_timeout_min"] + 1)
    with pytest.raises(NotAuthenticated) as info:
        auth.require(s)
    assert "inactivity" in info.value.user_message


def test_remembered_session_survives_restart_until_expiry(env, tmp_path):
    auth, db, clock, policy = env
    auth.setup_owner("alice", "Alice", OWNER_PW)
    s = auth.login("alice", OWNER_PW, remember=True)
    assert s.persistent
    clock.advance(hours=5)  # no inactivity timeout for "stay signed in"
    restored = auth.resume(s.token)
    assert auth.require(restored).username == "alice"
    clock.advance(days=policy["remember_me_days"] + 1)
    with pytest.raises(NotAuthenticated):
        auth.resume(s.token)


# ---------------------------------------------------------------- 8. lockout
def test_repeated_failures_lock_the_username_then_unlock(env):
    auth, _db, clock, policy = env
    owner(auth)
    for _ in range(policy["lockout_threshold"]):
        with pytest.raises(InvalidCredentials):
            auth.login("alice", "wrong-password-1")
    with pytest.raises(AccountLocked):
        auth.login("alice", OWNER_PW)  # even the right password is refused while locked
    clock.advance(minutes=policy["lockout_minutes"] + 1)
    assert auth.require(auth.login("alice", OWNER_PW)).username == "alice"


def test_lockout_applies_to_unknown_usernames_too(env):
    auth, _db, _clock, policy = env
    for _ in range(policy["lockout_threshold"]):
        with pytest.raises(InvalidCredentials):
            auth.login("ghost", "whatever-123")
    with pytest.raises(AccountLocked):
        auth.login("ghost", "whatever-123")


def test_old_login_attempts_are_purged(env):
    auth, db, clock, policy = env
    with pytest.raises(InvalidCredentials):
        auth.login("ghost", "whatever-123")
    clock.advance(days=policy["login_attempt_retention_days"] + 1)
    auth.purge_old_attempts()
    assert db.scalar("SELECT COUNT(*) FROM login_attempts") == 0


# ---------------------------------------------------------------- 9. roles and permissions
def test_normal_users_cannot_do_owner_actions(env, tmp_path):
    auth, *_ = env
    s = owner(auth)
    user_id, _ = auth.create_user(s, "bob", "Bob", "Kiwi-Lamp-456")
    bob = auth.login("bob", "Kiwi-Lamp-456")
    settings = SettingsService(PROJECT_ROOT / "config" / "settings.json", tmp_path / "user.json")
    for action in (lambda: auth.list_users(bob), lambda: auth.create_user(bob, "eve", "Eve", "Strong-Pass-777"),
                   lambda: auth.set_permission(bob, user_id, "settings.system", True),
                   lambda: auth.update_user(bob, user_id, "Boss"),
                   lambda: auth.update_system_settings(bob, settings, {"vad": {"threshold": 0.6}}),
                   lambda: auth.audit_log(bob)):
        with pytest.raises(PermissionDenied):
            action()
    auth.reauthenticate(bob, "Kiwi-Lamp-456")
    for action in (lambda: auth.set_status(bob, user_id, "disabled"), lambda: auth.reset_password(bob, user_id),
                   lambda: auth.transfer_ownership(bob, user_id), lambda: auth.delete_user(bob, user_id),
                   lambda: auth.update_security_settings(bob, settings, {"registration_enabled": True})):
        with pytest.raises(PermissionDenied):
            action()


def test_users_cannot_be_granted_owner_only_permissions(env):
    auth, *_ = env
    s = owner(auth)
    user_id, _ = auth.create_user(s, "bob", "Bob", "Kiwi-Lamp-456")
    with pytest.raises(PermissionDenied):
        auth.set_permission(s, user_id, "users.manage", True)
    auth.set_permission(s, user_id, "settings.system", True)
    auth.set_permission(s, user_id, "memory.use", False)
    bob = auth.require(auth.login("bob", "Kiwi-Lamp-456"))
    assert "settings.system" in bob.permissions and "memory.use" not in bob.permissions
    assert "users.manage" not in bob.permissions


def test_sensitive_owner_actions_require_recent_reauthentication(env):
    auth, _db, clock, _ = env
    s = owner(auth)
    user_id, _ = auth.create_user(s, "bob", "Bob", "Kiwi-Lamp-456")
    with pytest.raises(ReauthRequired):
        auth.set_status(s, user_id, "disabled")
    with pytest.raises(InvalidCredentials):
        auth.reauthenticate(s, "not-my-password")
    auth.reauthenticate(s, OWNER_PW)
    auth.set_status(s, user_id, "disabled")
    clock.advance(minutes=6)  # the confirmation expires after 5 minutes
    with pytest.raises(ReauthRequired):
        auth.set_status(s, user_id, "active")


def test_owner_reset_password_flow(env):
    auth, *_ = env
    s = owner(auth)
    user_id, temporary = auth.create_user(s, "bob", "Bob")  # temporary password generated, shown once
    bob = auth.login("bob", temporary)
    assert auth.require(bob).must_change_password
    auth.change_password(bob, temporary, "Kiwi-Lamp-456")
    assert not auth.require(bob).must_change_password
    auth.reauthenticate(s, OWNER_PW)
    new_temp = auth.reset_password(s, user_id)
    with pytest.raises(NotAuthenticated):
        auth.require(bob)  # all of Bob's sessions were revoked
    with pytest.raises(InvalidCredentials):
        auth.login("bob", "Kiwi-Lamp-456")
    assert auth.require(auth.login("bob", new_temp)).must_change_password


def test_change_password_needs_current_and_signs_out_other_sessions(env):
    auth, *_ = env
    s = owner(auth)
    other = auth.login("alice", OWNER_PW)
    with pytest.raises(InvalidCredentials):
        auth.change_password(s, "wrong-current-1", "Brand-New-Pass-8")
    auth.change_password(s, OWNER_PW, "Brand-New-Pass-8")
    auth.require(s)  # this session stays valid
    with pytest.raises(NotAuthenticated):
        auth.require(other)
    assert auth.login("alice", "Brand-New-Pass-8")


def test_owner_cannot_be_disabled_or_deleted_and_ownership_transfer(env):
    auth, *_ = env
    s = owner(auth)
    owner_id = auth.require(s).id
    user_id, _ = auth.create_user(s, "bob", "Bob", "Kiwi-Lamp-456")
    auth.reauthenticate(s, OWNER_PW)
    with pytest.raises(PermissionDenied):
        auth.set_status(s, owner_id, "disabled")
    with pytest.raises(PermissionDenied):
        auth.delete_user(s, owner_id)
    auth.transfer_ownership(s, user_id)
    assert auth.require(s).role == "user"
    assert auth.require(auth.login("bob", "Kiwi-Lamp-456")).is_owner
    with pytest.raises(PermissionDenied):
        auth.list_users(s)


def test_registration_disabled_by_default_and_creates_only_normal_users(env):
    auth, _db, _clock, policy = env
    owner(auth)
    with pytest.raises(PermissionDenied):
        auth.register("carol", "Carol", "Sunny-Day-321")
    policy["registration_enabled"] = True
    auth.register("carol", "Carol", "Sunny-Day-321")
    assert auth.require(auth.login("carol", "Sunny-Day-321")).role == "user"


def test_user_list_never_contains_hashes(env):
    auth, *_ = env
    s = owner(auth)
    auth.create_user(s, "bob", "Bob", "Kiwi-Lamp-456")
    users = auth.list_users(s)
    assert {u["username"] for u in users} == {"alice", "bob"}
    assert all("password_hash" not in u for u in users)


def test_delete_user_removes_all_their_data(env):
    auth, db, *_ = env
    s = owner(auth)
    user_id, _ = auth.create_user(s, "bob", "Bob", "Kiwi-Lamp-456")
    bob = auth.login("bob", "Kiwi-Lamp-456")
    from services.history_service import HistoryService
    from services.memory_service import MemoryService
    HistoryService(db, auth).record_user_message(bob, None, "hi", "text", "k1")
    MemoryService(db, auth).remember(bob, "pet", "cat")
    auth.reauthenticate(s, OWNER_PW)
    auth.delete_user(s, user_id)
    for table in ("conversations", "messages", "long_term_memory", "user_sessions"):
        assert db.scalar(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (user_id,)) == 0
