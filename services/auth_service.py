"""Accounts, login sessions, permissions and owner administration.

Security model (enforced here, in the service layer, not just in the dashboard):

* Passwords are stored only as Argon2id hashes (``security/passwords.py``).
* A login returns a ``Session`` holding a random token. The database stores
  only the token's SHA-256 hash. **Every** private operation calls
  ``require(session)``, which looks the token up, checks expiry/inactivity/
  revocation and that the account is still active, and returns the user. The
  user id therefore always comes from the database, never from the caller.
* Failed logins are counted per username (including unknown usernames, so the
  lockout cannot be used to discover which accounts exist). Error messages are
  generic.
* There is exactly one owner (a unique index enforces it). The owner is created
  through the first-run setup only while no owner exists. No default account,
  no hardcoded password.
* Sensitive owner actions (disable/reactivate, password reset, ownership
  transfer, deleting an account, changing account security settings) require
  the owner to re-enter their password within the last 5 minutes.
* Account management does not grant access to other users' conversations or
  memories; no method here reads private content.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
import string
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from security import passwords as pw
from storage.local_database import LocalDatabase

log = logging.getLogger(__name__)

REAUTH_WINDOW_S = 300
NON_PERSISTENT_MAX_AGE = timedelta(hours=12)

PERMISSIONS: dict[str, str] = {
    "ai.chat": "Talk to DUMBOO's AI (text and voice)",
    "memory.use": "Long-term memory (save and recall facts)",
    "history.export": "Export own conversations and memories",
    "tasks.manage": "Own tasks and schedule",
    "system.view": "See System and Diagnostics pages",
    "settings.system": "Change system settings (audio, wake word, AI provider)",
    "users.manage": "Manage user accounts (owner only)",
    # Module 4: laptop automation
    "automation.basic": "Automation: open/switch apps, open websites, read system status, find files",
    "automation.files": "Automation: create, rename, copy, move and organise files in permitted folders",
    "automation.input": "Automation: keyboard, mouse, clipboard and reading the screen",
    "automation.web": "Automation: DUMBOO's browser (search results, tabs, downloads, forms)",
    "automation.dev": "Automation: run approved project commands (tests, dependency installs)",
    "automation.admin": "Automation: approve applications and permitted folders (owner only)",
    # Module 5
    "research.web": "Live web search, news and research (sends search queries to the internet)",
    # Module 7
    "security.view": "Security Guardian: see this PC's security status, startup programs, processes and network",
    "security.manage": "Security Guardian: disable/restore startup entries after confirmation (owner only)",
}
OWNER_ONLY = {"users.manage", "automation.admin", "security.manage"}
ROLE_DEFAULTS: dict[str, set[str]] = {
    "owner": set(PERMISSIONS),
    "user": {"ai.chat", "memory.use", "history.export", "tasks.manage", "system.view", "automation.basic",
             "automation.files", "automation.input", "automation.web", "research.web"},
}


# ------------------------------------------------------------------ errors
class AuthError(Exception):
    user_message = "Access denied."

    def __init__(self, user_message: str | None = None) -> None:
        self.user_message = user_message or self.user_message
        super().__init__(self.user_message)


class NotAuthenticated(AuthError):
    user_message = "Please sign in."


class InvalidCredentials(AuthError):
    user_message = "Invalid username or password."


class AccountLocked(AuthError):
    def __init__(self, retry_after_s: int) -> None:
        self.retry_after_s = retry_after_s
        minutes = max(1, (retry_after_s + 59) // 60)
        super().__init__(f"Too many failed sign-in attempts. Try again in {minutes} minute(s).")


class AccountDisabled(AuthError):
    user_message = "This account is disabled. Ask the owner to reactivate it."


class PermissionDenied(AuthError):
    user_message = "You do not have permission to do that."


class ReauthRequired(AuthError):
    user_message = "Please confirm your password to continue."


class SetupError(AuthError):
    user_message = "Owner setup is not possible."


class AccountValidationError(AuthError):
    pass


# ------------------------------------------------------------------ data
@dataclass
class Session:
    """Proof of a successful login. Only ``token`` matters; the other fields are display hints."""

    token: str = field(repr=False)
    username: str = ""
    display_name: str = ""
    role: str = ""
    persistent: bool = False

    @property
    def token_hash(self) -> str:
        return _hash_token(self.token)


@dataclass(frozen=True)
class AuthenticatedUser:
    id: int
    username: str
    display_name: str
    role: str
    must_change_password: bool
    permissions: frozenset[str]

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"

    def can(self, permission: str) -> bool:
        return permission in self.permissions


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I


def generate_recovery_code() -> str:
    """16 characters from a 32-letter alphabet (80 bits), shown as XXXX-XXXX-XXXX-XXXX."""
    raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(16))
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def normalize_recovery_code(code: str) -> str:
    cleaned = "".join(c for c in (code or "").upper() if c.isalnum())
    return "-".join(cleaned[i:i + 4] for i in range(0, len(cleaned), 4))


def generate_temporary_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.isdigit() for c in candidate) and any(c.isupper() for c in candidate):
            return candidate


# ------------------------------------------------------------------ service
class AuthService:
    name = "auth"

    def __init__(self, db: LocalDatabase, policy: Callable[[], dict], hasher=None,
                 clock: Callable[[], datetime] = datetime.now, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._db = db
        self._policy = policy          # returns the "accounts" settings section
        self._hasher = hasher or pw.make_hasher()
        self._clock = clock
        self._mono = monotonic
        self._reauth: dict[str, float] = {}   # token hash -> monotonic deadline
        self._lock = threading.Lock()
        self._dummy_hash = self._hasher.hash(secrets.token_hex(16))  # equalises timing for unknown users

    # -- helpers ---------------------------------------------------------------
    def _now(self) -> datetime:
        return self._clock()

    def _iso(self, dt: datetime | None = None) -> str:
        return (dt or self._now()).isoformat(timespec="seconds")

    def _audit(self, conn: sqlite3.Connection, actor: int | None, action: str, target: int | None = None,
               **metadata: Any) -> None:
        conn.execute("INSERT INTO admin_audit (actor_user_id, action, target_user_id, timestamp, metadata) "
                     "VALUES (?, ?, ?, ?, ?)", (actor, action, target, self._iso(), json.dumps(metadata)))

    def _permissions_for(self, user_id: int, role: str) -> frozenset[str]:
        perms = set(ROLE_DEFAULTS[role])
        for row in self._db.query("SELECT permission, granted FROM user_permissions WHERE user_id = ?", (user_id,)):
            if row["permission"] in OWNER_ONLY and role != "owner":
                continue
            (perms.add if row["granted"] else perms.discard)(row["permission"])
        if role == "owner":
            perms |= OWNER_ONLY | {"settings.system"}
        return frozenset(perms)

    # -- first run -------------------------------------------------------------------
    def owner_exists(self) -> bool:
        return bool(self._db.scalar("SELECT COUNT(*) FROM users WHERE role = 'owner'"))

    def needs_owner_setup(self) -> bool:
        return not self.owner_exists()

    def setup_owner(self, username: str, display_name: str, password: str, title: str | None = None) -> int:
        """First-run only: create the single owner account. Existing tasks/events are adopted by the owner."""
        username = self._valid_username(username)
        display_name = self._valid_display(display_name)
        self._valid_password(password, username)
        now = self._iso()
        password_hash = self._hasher.hash(password)
        try:
            with self._db.transaction() as conn:
                if conn.execute("SELECT COUNT(*) FROM users WHERE role = 'owner'").fetchone()[0]:
                    raise SetupError("An owner account already exists. Sign in instead.")
                cur = conn.execute(
                    "INSERT INTO users (username, username_norm, password_hash, display_name, role, created_at, "
                    "updated_at, password_changed_at) VALUES (?, ?, ?, ?, 'owner', ?, ?, ?)",
                    (username, pw.normalize_username(username), password_hash, display_name, now, now, now))
                owner_id = cur.lastrowid
                conn.execute("INSERT INTO user_preferences (user_id, preference_key, preference_value, created_at, "
                             "updated_at) VALUES (?, 'title', ?, ?, ?)",
                             (owner_id, (title or self._policy()["owner_title_default"]).strip(), now, now))
                adopted_tasks = conn.execute("UPDATE tasks SET user_id = ? WHERE user_id IS NULL", (owner_id,)).rowcount
                adopted_events = conn.execute("UPDATE events SET user_id = ? WHERE user_id IS NULL", (owner_id,)).rowcount
                self._audit(conn, owner_id, "owner_setup", owner_id, adopted_tasks=adopted_tasks,
                            adopted_events=adopted_events)
        except sqlite3.IntegrityError as exc:
            raise SetupError("That username is already taken, or an owner already exists.") from exc
        log.info("Owner account created")
        return owner_id

    # -- validation ---------------------------------------------------------------------
    @staticmethod
    def _valid_username(username: str) -> str:
        try:
            return pw.check_username(username)
        except pw.PasswordPolicyError as exc:
            raise AccountValidationError(str(exc)) from exc

    @staticmethod
    def _valid_display(display_name: str) -> str:
        display_name = " ".join((display_name or "").split())
        if not 1 <= len(display_name) <= 60:
            raise AccountValidationError("Display name must be 1-60 characters.")
        return display_name

    @staticmethod
    def _valid_password(password: str, username: str) -> None:
        try:
            pw.check_password(password, username)
        except pw.PasswordPolicyError as exc:
            raise AccountValidationError(str(exc)) from exc

    # -- login / logout ------------------------------------------------------------------
    def _lockout_remaining(self, username_norm: str) -> int:
        policy = self._policy()
        window_start = self._now() - timedelta(minutes=policy["lockout_minutes"])
        rows = self._db.query(
            "SELECT success, attempted_at FROM login_attempts WHERE username_norm = ? AND attempted_at >= ? "
            "ORDER BY id DESC", (username_norm, self._iso(window_start)))
        failures = []
        for r in rows:
            if r["success"]:
                break  # only failures since the last success count
            failures.append(r["attempted_at"])
        if len(failures) < policy["lockout_threshold"]:
            return 0
        newest = datetime.fromisoformat(failures[0])
        unlock = newest + timedelta(minutes=policy["lockout_minutes"])
        return max(1, int((unlock - self._now()).total_seconds()))

    def login(self, username: str, password: str, remember: bool = False) -> Session:
        norm = pw.normalize_username(username)
        if not norm or not password:
            raise InvalidCredentials()
        remaining = self._lockout_remaining(norm)
        if remaining:
            raise AccountLocked(remaining)
        user = self._db.row("SELECT * FROM users WHERE username_norm = ?", (norm,))
        ok = pw.verify(self._hasher, user["password_hash"] if user else self._dummy_hash, password) and user is not None
        self._db.execute("INSERT INTO login_attempts (username_norm, success, attempted_at) VALUES (?, ?, ?)",
                         (norm, 1 if ok else 0, self._iso()))
        if not ok:
            log.info("Failed sign-in attempt")
            raise InvalidCredentials()
        if user["status"] != "active":
            raise AccountDisabled()
        policy = self._policy()
        persistent = bool(remember and policy["remember_me_allowed"])
        token = secrets.token_urlsafe(32)
        now = self._now()
        expires = now + (timedelta(days=policy["remember_me_days"]) if persistent else NON_PERSISTENT_MAX_AGE)
        with self._db.transaction() as conn:
            if self._hasher.check_needs_rehash(user["password_hash"]):
                conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (self._hasher.hash(password), user["id"]))
            conn.execute("INSERT INTO user_sessions (user_id, token_hash, persistent, created_at, last_seen_at, "
                         "expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                         (user["id"], _hash_token(token), 1 if persistent else 0, self._iso(now), self._iso(now),
                          self._iso(expires)))
            conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (self._iso(now), user["id"]))
        log.info("User signed in (role=%s)", user["role"])
        return Session(token, user["username"], user["display_name"], user["role"], persistent)

    def login_verified(self, user_id: int, method: str) -> Session:
        """Open a session for a user whose identity was verified another way (face unlock).

        Never "stay signed in", and the account must be active. Recorded like a password sign-in.
        Re-authentication for sensitive actions still needs the password.
        """
        user = self._db.row("SELECT * FROM users WHERE id = ?", (int(user_id),))
        if user is None:
            raise InvalidCredentials()
        if user["status"] != "active":
            raise AccountDisabled()
        token = secrets.token_urlsafe(32)
        now = self._now()
        with self._db.transaction() as conn:
            conn.execute("INSERT INTO login_attempts (username_norm, success, attempted_at) VALUES (?, 1, ?)",
                         (user["username_norm"], self._iso(now)))
            conn.execute("INSERT INTO user_sessions (user_id, token_hash, persistent, created_at, last_seen_at, "
                         "expires_at) VALUES (?, ?, 0, ?, ?, ?)",
                         (user["id"], _hash_token(token), self._iso(now), self._iso(now),
                          self._iso(now + NON_PERSISTENT_MAX_AGE)))
            conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (self._iso(now), user["id"]))
        log.info("User signed in by %s (role=%s)", method, user["role"])
        return Session(token, user["username"], user["display_name"], user["role"], False)

    def resume(self, token: str) -> Session:
        """Restore a remembered session (stay signed in). Fully validated like any other request."""
        session = Session(token)
        user = self.require(session)
        return Session(token, user.username, user.display_name, user.role, True)

    def logout(self, session: Session | None, reason: str = "logout") -> None:
        if session is None:
            return
        self._db.execute("UPDATE user_sessions SET revoked_at = ?, revoke_reason = ? WHERE token_hash = ? "
                         "AND revoked_at IS NULL", (self._iso(), reason, session.token_hash))
        with self._lock:
            self._reauth.pop(session.token_hash, None)

    def require(self, session: Session | None, permission: str | None = None) -> AuthenticatedUser:
        """Validate the session and (optionally) a permission. Returns the user from the database."""
        if session is None or not getattr(session, "token", None):
            raise NotAuthenticated()
        row = self._db.row(
            "SELECT s.id AS session_id, s.persistent, s.last_seen_at, s.expires_at, s.revoked_at, u.* "
            "FROM user_sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?", (session.token_hash,))
        if row is None or row["revoked_at"]:
            raise NotAuthenticated("Your session has ended. Please sign in again.")
        now = self._now()
        if now >= datetime.fromisoformat(row["expires_at"]):
            self._revoke_id(row["session_id"], "expired")
            raise NotAuthenticated("Your session expired. Please sign in again.")
        idle_limit = timedelta(minutes=self._policy()["session_timeout_min"])
        if not row["persistent"] and now - datetime.fromisoformat(row["last_seen_at"]) > idle_limit:
            self._revoke_id(row["session_id"], "inactivity")
            raise NotAuthenticated("You were signed out after a period of inactivity.")
        if row["status"] != "active":
            self._revoke_id(row["session_id"], "account disabled")
            raise AccountDisabled()
        if (now - datetime.fromisoformat(row["last_seen_at"])).total_seconds() > 20:
            self._db.execute("UPDATE user_sessions SET last_seen_at = ? WHERE id = ?", (self._iso(now), row["session_id"]))
        user = AuthenticatedUser(row["id"], row["username"], row["display_name"], row["role"],
                                 bool(row["must_change_password"]), self._permissions_for(row["id"], row["role"]))
        if permission is not None and not user.can(permission):
            raise PermissionDenied()
        return user

    def touch(self, session: Session | None) -> None:
        """Record activity (keeps the inactivity timer from expiring while the user is active)."""
        if session is not None:
            self._db.execute("UPDATE user_sessions SET last_seen_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                             (self._iso(), session.token_hash))

    def _revoke_id(self, session_id: int, reason: str) -> None:
        self._db.execute("UPDATE user_sessions SET revoked_at = ?, revoke_reason = ? WHERE id = ? AND revoked_at IS NULL",
                         (self._iso(), reason, session_id))

    # -- re-authentication -------------------------------------------------------------------
    def reauthenticate(self, session: Session, password: str) -> None:
        user = self.require(session)
        row = self._db.row("SELECT password_hash FROM users WHERE id = ?", (user.id,))
        if not pw.verify(self._hasher, row["password_hash"], password):
            self._db.execute("INSERT INTO login_attempts (username_norm, success, attempted_at) VALUES (?, 0, ?)",
                             (pw.normalize_username(user.username), self._iso()))
            raise InvalidCredentials("Incorrect password.")
        with self._lock:
            self._reauth[session.token_hash] = self._mono() + REAUTH_WINDOW_S

    def _require_recent(self, session: Session, permission: str) -> AuthenticatedUser:
        user = self.require(session, permission)
        with self._lock:
            deadline = self._reauth.get(session.token_hash, 0)
        if self._mono() > deadline:
            raise ReauthRequired()
        return user

    # -- own account ---------------------------------------------------------------------------
    def change_password(self, session: Session, current: str, new: str) -> None:
        user = self.require(session)
        row = self._db.row("SELECT password_hash FROM users WHERE id = ?", (user.id,))
        if not pw.verify(self._hasher, row["password_hash"], current):
            raise InvalidCredentials("Current password is incorrect.")
        if current == new:
            raise AccountValidationError("The new password must be different.")
        self._valid_password(new, user.username)
        now = self._iso()
        with self._db.transaction() as conn:
            conn.execute("UPDATE users SET password_hash = ?, must_change_password = 0, password_changed_at = ?, "
                         "updated_at = ? WHERE id = ?", (self._hasher.hash(new), now, now, user.id))
            # Sign out every other device/session of this user.
            conn.execute("UPDATE user_sessions SET revoked_at = ?, revoke_reason = 'password changed' "
                         "WHERE user_id = ? AND token_hash != ? AND revoked_at IS NULL",
                         (now, user.id, session.token_hash))
            self._audit(conn, user.id, "password_change", user.id)

    def update_own_display_name(self, session: Session, display_name: str) -> None:
        user = self.require(session)
        self._db.execute("UPDATE users SET display_name = ?, updated_at = ? WHERE id = ?",
                         (self._valid_display(display_name), self._iso(), user.id))

    def register(self, username: str, display_name: str, password: str) -> int:
        """Self-registration (only when the owner enabled it). Always creates a normal user."""
        if not self._policy()["registration_enabled"]:
            raise PermissionDenied("Registration is disabled. Ask the owner to create your account.")
        if self.needs_owner_setup():
            raise PermissionDenied("The owner account must be set up first.")
        return self._insert_user(None, username, display_name, password, must_change=False)

    # -- owner administration -------------------------------------------------------------------
    def _insert_user(self, actor: int | None, username: str, display_name: str, password: str,
                     must_change: bool) -> int:
        username = self._valid_username(username)
        display_name = self._valid_display(display_name)
        self._valid_password(password, username)
        now = self._iso()
        password_hash = self._hasher.hash(password)
        try:
            with self._db.transaction() as conn:
                cur = conn.execute(
                    "INSERT INTO users (username, username_norm, password_hash, display_name, role, "
                    "must_change_password, created_at, updated_at, password_changed_at) "
                    "VALUES (?, ?, ?, ?, 'user', ?, ?, ?, ?)",
                    (username, pw.normalize_username(username), password_hash, display_name,
                     1 if must_change else 0, now, now, now))
                self._audit(conn, actor, "user_create" if actor else "user_register", cur.lastrowid)
                return cur.lastrowid
        except sqlite3.IntegrityError as exc:
            raise AccountValidationError("That username is already taken.") from exc

    def create_user(self, session: Session, username: str, display_name: str,
                    password: str | None = None) -> tuple[int, str | None]:
        """Owner creates a normal user. Without a password a temporary one is generated (shown once)."""
        owner = self.require(session, "users.manage")
        temporary = None
        if not password:
            temporary = password = generate_temporary_password()
        user_id = self._insert_user(owner.id, username, display_name, password, must_change=True)
        return user_id, temporary

    def list_users(self, session: Session) -> list[dict]:
        """Account overview for the owner. Never includes password hashes or private content."""
        self.require(session, "users.manage")
        rows = self._db.query(
            "SELECT id, username, display_name, role, status, must_change_password, created_at, updated_at, "
            "last_login_at FROM users ORDER BY role = 'owner' DESC, username_norm")
        for row in rows:
            row["locked"] = bool(self._lockout_remaining(pw.normalize_username(row["username"])))
            row["permissions"] = sorted(self._permissions_for(row["id"], row["role"]))
        return rows

    def _target(self, user_id: int) -> dict:
        row = self._db.row("SELECT id, username, role, status FROM users WHERE id = ?", (int(user_id),))
        if row is None:
            raise AccountValidationError("That account does not exist.")
        return row

    def update_user(self, session: Session, user_id: int, display_name: str) -> None:
        owner = self.require(session, "users.manage")
        self._target(user_id)
        with self._db.transaction() as conn:
            conn.execute("UPDATE users SET display_name = ?, updated_at = ? WHERE id = ?",
                         (self._valid_display(display_name), self._iso(), int(user_id)))
            self._audit(conn, owner.id, "user_update", int(user_id))

    def set_status(self, session: Session, user_id: int, status: str) -> None:
        owner = self._require_recent(session, "users.manage")
        if status not in ("active", "disabled"):
            raise AccountValidationError("Status must be active or disabled.")
        target = self._target(user_id)
        if target["role"] == "owner":
            raise PermissionDenied("The owner account cannot be disabled.")
        now = self._iso()
        with self._db.transaction() as conn:
            conn.execute("UPDATE users SET status = ?, updated_at = ? WHERE id = ?", (status, now, target["id"]))
            if status == "disabled":
                conn.execute("UPDATE user_sessions SET revoked_at = ?, revoke_reason = 'account disabled' "
                             "WHERE user_id = ? AND revoked_at IS NULL", (now, target["id"]))
            self._audit(conn, owner.id, f"user_{status}", target["id"])

    def reset_password(self, session: Session, user_id: int) -> str:
        """Owner-initiated reset: returns a one-time temporary password the user must change at next sign-in."""
        owner = self._require_recent(session, "users.manage")
        target = self._target(user_id)
        if target["id"] == owner.id:
            raise PermissionDenied("Use 'Change password' for your own account.")
        temporary = generate_temporary_password()
        now = self._iso()
        with self._db.transaction() as conn:
            conn.execute("UPDATE users SET password_hash = ?, must_change_password = 1, password_changed_at = ?, "
                         "updated_at = ? WHERE id = ?", (self._hasher.hash(temporary), now, now, target["id"]))
            conn.execute("UPDATE user_sessions SET revoked_at = ?, revoke_reason = 'password reset' "
                         "WHERE user_id = ? AND revoked_at IS NULL", (now, target["id"]))
            self._audit(conn, owner.id, "password_reset", target["id"])
        return temporary

    def set_permission(self, session: Session, user_id: int, permission: str, granted: bool) -> None:
        owner = self.require(session, "users.manage")
        if permission not in PERMISSIONS:
            raise AccountValidationError("Unknown permission.")
        if permission in OWNER_ONLY:
            raise PermissionDenied("That permission belongs to the owner only.")
        target = self._target(user_id)
        if target["role"] == "owner":
            raise PermissionDenied("The owner always has every permission.")
        now = self._iso()
        with self._db.transaction() as conn:
            conn.execute("INSERT INTO user_permissions (user_id, permission, granted, created_at, updated_at) "
                         "VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id, permission) DO UPDATE SET "
                         "granted = excluded.granted, updated_at = excluded.updated_at",
                         (target["id"], permission, 1 if granted else 0, now, now))
            self._audit(conn, owner.id, "permission_set", target["id"], permission=permission, granted=granted)

    def transfer_ownership(self, session: Session, user_id: int) -> None:
        """Make another active user the owner; the current owner becomes a normal user."""
        owner = self._require_recent(session, "users.manage")
        target = self._target(user_id)
        if target["id"] == owner.id or target["status"] != "active":
            raise AccountValidationError("Choose another, active account.")
        now = self._iso()
        with self._db.transaction() as conn:
            conn.execute("UPDATE users SET role = 'user', updated_at = ? WHERE id = ?", (now, owner.id))
            conn.execute("UPDATE users SET role = 'owner', updated_at = ? WHERE id = ?", (now, target["id"]))
            self._audit(conn, owner.id, "ownership_transfer", target["id"])

    def delete_user(self, session: Session, user_id: int) -> None:
        """Delete an account and ALL of its data (conversations, memories, tasks, events)."""
        owner = self._require_recent(session, "users.manage")
        target = self._target(user_id)
        if target["role"] == "owner":
            raise PermissionDenied("The owner account cannot be deleted. Transfer ownership first.")
        with self._db.transaction() as conn:
            self._audit(conn, owner.id, "user_delete", None, username=target["username"])
            conn.execute("DELETE FROM users WHERE id = ?", (target["id"],))

    def update_system_settings(self, session: Session, settings_service, changes: dict) -> list[str]:
        """Audio / wake word / AI / dashboard settings. Needs the 'settings.system' permission."""
        user = self.require(session, "settings.system")
        if "accounts" in changes:
            raise PermissionDenied("Account security settings are changed on the ADMIN page.")
        changed = settings_service.update(changes)
        if changed:
            with self._db.transaction() as conn:
                self._audit(conn, user.id, "system_settings", None, changed=changed)
        return changed

    def update_security_settings(self, session: Session, settings_service, changes: dict) -> list[str]:
        """Account/privacy settings (registration, timeouts, lockout, retention). Owner + re-auth."""
        owner = self._require_recent(session, "users.manage")
        changed = settings_service.update({"accounts": changes})
        with self._db.transaction() as conn:
            self._audit(conn, owner.id, "security_settings", None, changed=changed)
        return changed

    def audit_log(self, session: Session, limit: int = 100) -> list[dict]:
        self.require(session, "users.manage")
        return self._db.query(
            "SELECT a.timestamp, a.action, actor.username AS actor, target.username AS target, a.metadata "
            "FROM admin_audit a LEFT JOIN users actor ON actor.id = a.actor_user_id "
            "LEFT JOIN users target ON target.id = a.target_user_id ORDER BY a.id DESC LIMIT ?", (limit,))

    # -- password recovery --------------------------------------------------------------------------
    def has_recovery_code(self, session: Session) -> bool:
        user = self.require(session)
        return bool(self._db.scalar("SELECT COUNT(*) FROM recovery_codes WHERE user_id = ?", (user.id,)))

    def create_recovery_code(self, session: Session, password: str) -> str:
        """A new recovery code for your own account (replaces the old one). Needs your password."""
        user = self.require(session)
        row = self._db.row("SELECT password_hash FROM users WHERE id = ?", (user.id,))
        if not pw.verify(self._hasher, row["password_hash"], password):
            raise InvalidCredentials("Password is incorrect.")
        with self._db.transaction() as conn:
            code = self._issue_recovery_code(conn, user.id)
            self._audit(conn, user.id, "recovery_code_created", user.id)
        return code

    def _issue_recovery_code(self, conn: sqlite3.Connection, user_id: int) -> str:
        code = generate_recovery_code()
        conn.execute("INSERT INTO recovery_codes (user_id, code_hash, created_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(user_id) DO UPDATE SET code_hash = excluded.code_hash, "
                     "created_at = excluded.created_at", (user_id, self._hasher.hash(code), self._iso()))
        return code

    def recover_with_code(self, username: str, code: str, new_password: str) -> str:
        """Forgot password: set a new one with the recovery code. The code is used up and a new one is returned.

        Counts as a sign-in attempt, so the normal lockout stops guessing. All sessions are signed out.
        """
        norm = pw.normalize_username(username)
        code = normalize_recovery_code(code)
        if not norm or not code:
            raise InvalidCredentials("Enter your username and recovery code.")
        remaining = self._lockout_remaining(norm)
        if remaining:
            raise AccountLocked(remaining)
        user = self._db.row("SELECT u.id, u.username, u.status, r.code_hash FROM users u LEFT JOIN recovery_codes r "
                            "ON r.user_id = u.id WHERE u.username_norm = ?", (norm,))
        stored = user["code_hash"] if user and user["code_hash"] else self._dummy_hash
        ok = pw.verify(self._hasher, stored, code) and user is not None and bool(user["code_hash"])
        if not ok:
            self._db.execute("INSERT INTO login_attempts (username_norm, success, attempted_at) VALUES (?, 0, ?)",
                             (norm, self._iso()))
            log.info("Failed password recovery attempt")
            raise InvalidCredentials("That username and recovery code don't match.")
        if user["status"] != "active":
            raise AccountDisabled()
        self._valid_password(new_password, user["username"])
        return self._set_new_password(user["id"], new_password, "password_recovered")

    def reset_password_locally(self, username: str, new_password: str) -> str:
        """For tools/reset_password.py, run by hand on this PC (never reachable from the app or the phone)."""
        user = self._db.row("SELECT id, username FROM users WHERE username_norm = ?",
                            (pw.normalize_username(username),))
        if user is None:
            raise InvalidCredentials("No account with that username.")
        self._valid_password(new_password, user["username"])
        return self._set_new_password(user["id"], new_password, "password_reset_local")

    def _set_new_password(self, user_id: int, new_password: str, action: str) -> str:
        now = self._iso()
        with self._db.transaction() as conn:
            conn.execute("UPDATE users SET password_hash = ?, must_change_password = 0, password_changed_at = ?, "
                         "updated_at = ? WHERE id = ?", (self._hasher.hash(new_password), now, now, user_id))
            conn.execute("UPDATE user_sessions SET revoked_at = ?, revoke_reason = 'password recovered' "
                         "WHERE user_id = ? AND revoked_at IS NULL", (now, user_id))
            conn.execute("DELETE FROM login_attempts WHERE username_norm = (SELECT username_norm FROM users "
                         "WHERE id = ?) AND success = 0", (user_id,))
            code = self._issue_recovery_code(conn, user_id)
            self._audit(conn, user_id, action, user_id)
        log.info("Password reset (%s)", action)
        return code

    # -- housekeeping -------------------------------------------------------------------------------
    def purge_old_attempts(self) -> int:
        cutoff = self._now() - timedelta(days=self._policy()["login_attempt_retention_days"])
        removed = self._db.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (self._iso(cutoff),))
        self._db.execute("DELETE FROM user_sessions WHERE (revoked_at IS NOT NULL AND revoked_at < ?) "
                         "OR expires_at < ?", (self._iso(cutoff), self._iso(cutoff)))
        return removed
