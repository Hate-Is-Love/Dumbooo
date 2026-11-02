"""Password hashing with Argon2id (``argon2-cffi``) and password/username rules.

Only the Argon2id hash is stored. It contains its own random salt and cost
parameters, so hashes made with older settings keep working and are upgraded
automatically at the next successful login (``needs_rehash``).
"""

from __future__ import annotations

import re

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")
MIN_PASSWORD = 8
MAX_PASSWORD = 128
_COMMON = {"password", "password1", "12345678", "123456789", "qwerty123", "iloveyou", "11111111", "dumboo123",
           "letmein1", "admin123", "welcome1", "abcdefgh"}


class PasswordPolicyError(ValueError):
    pass


def make_hasher(fast: bool = False) -> PasswordHasher:
    """Argon2id with OWASP-recommended cost (64 MiB, 3 passes). ``fast`` is for tests only."""
    if fast:
        return PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1, type=Type.ID)
    return PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, type=Type.ID)


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def check_username(username: str) -> str:
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise PasswordPolicyError("Username must be 3-32 characters: letters, digits, dot, dash or underscore, "
                                  "starting with a letter or digit.")
    return username


def check_password(password: str, username: str = "") -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise PasswordPolicyError(f"Password must be at least {MIN_PASSWORD} characters.")
    if len(password) > MAX_PASSWORD:
        raise PasswordPolicyError(f"Password must be at most {MAX_PASSWORD} characters.")
    if password.strip() != password:
        raise PasswordPolicyError("Password must not start or end with a space.")
    low = password.lower()
    if low in _COMMON or (username and normalize_username(username) in low):
        raise PasswordPolicyError("That password is too easy to guess. Choose a different one.")
    if len(set(password)) < 4:
        raise PasswordPolicyError("That password is too simple. Use more different characters.")


def verify(hasher: PasswordHasher, stored_hash: str, password: str) -> bool:
    try:
        return hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
