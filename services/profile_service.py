"""Per-user preferences (title, answer length, voice output).

Every call needs a valid ``Session``; the user comes from ``AuthService.require``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from services.auth_service import AccountValidationError, AuthService, Session
from storage.local_database import LocalDatabase

PREFERENCES: dict[str, tuple[str, ...] | None] = {
    "title": None,                          # free text: how DUMBOO addresses you ("Sir", "Anya" …)
    "response_length": ("short", "detailed"),
    "voice_output": ("on", "off"),
    "greeting": ("on", "off"),             # greet and (first time) introduce DUMBOO after signing in
    "introduced": ("yes", "no"),            # DUMBOO has introduced itself to this user once
}


class ProfileService:
    def __init__(self, db: LocalDatabase, auth: AuthService, policy: Callable[[], dict],
                 clock: Callable[[], datetime] = datetime.now) -> None:
        self._db = db
        self._auth = auth
        self._policy = policy
        self._clock = clock

    def defaults(self, role: str, display_name: str) -> dict[str, str]:
        title = self._policy()["owner_title_default"] if role == "owner" else display_name
        return {"title": title, "response_length": "short", "voice_output": "on", "greeting": "on",
                "introduced": "no"}

    def get(self, session: Session) -> dict[str, str]:
        user = self._auth.require(session)
        prefs = self.defaults(user.role, user.display_name)
        for row in self._db.query("SELECT preference_key, preference_value FROM user_preferences WHERE user_id = ?",
                                  (user.id,)):
            if row["preference_key"] in PREFERENCES:
                prefs[row["preference_key"]] = row["preference_value"]
        return prefs

    def set(self, session: Session, key: str, value: str) -> None:
        user = self._auth.require(session)
        if key not in PREFERENCES:
            raise AccountValidationError("Unknown preference.")
        value = " ".join(str(value).split())
        choices = PREFERENCES[key]
        if choices is not None and value not in choices:
            raise AccountValidationError(f"{key} must be one of: {', '.join(choices)}.")
        if key == "title" and not 1 <= len(value) <= 40:
            raise AccountValidationError("Title must be 1-40 characters.")
        now = self._clock().isoformat(timespec="seconds")
        self._db.execute(
            "INSERT INTO user_preferences (user_id, preference_key, preference_value, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id, preference_key) DO UPDATE SET "
            "preference_value = excluded.preference_value, updated_at = excluded.updated_at",
            (user.id, key, value, now, now))
