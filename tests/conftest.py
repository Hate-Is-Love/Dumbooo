import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from utils.paths import AppPaths  # noqa: E402


@pytest.fixture
def paths(tmp_path) -> AppPaths:
    """Project paths with runtime data, logs and models redirected to a temp folder."""
    return AppPaths(ROOT, tmp_path / "data", tmp_path / "logs", tmp_path / "models").ensure()


POLICY = {
    "owner_title_default": "Sir", "registration_enabled": False, "session_timeout_min": 30, "lockout_threshold": 5,
    "lockout_minutes": 15, "login_attempt_retention_days": 30, "remember_me_allowed": True, "remember_me_days": 7,
    "history_retention_days": 0, "memory_auto_extract": True, "memory_ai_learning": True, "face_unlock_enabled": True, "retrieval_memories": 6, "retrieval_messages": 4,
}


class Accounts:
    """A temporary database with an owner ('alice') and a normal user ('bob'), both signed in."""

    def __init__(self, db_path: Path) -> None:
        from security.passwords import make_hasher
        from services.auth_service import AuthService
        from storage.local_database import LocalDatabase

        self.policy = dict(POLICY)
        self.db = LocalDatabase(db_path)
        self.auth = AuthService(self.db, lambda: self.policy, hasher=make_hasher(fast=True))
        self.owner_id = self.auth.setup_owner("alice", "Alice", "Owner-Pass-123")
        self.owner = self.auth.login("alice", "Owner-Pass-123")
        self.user_id, _ = self.auth.create_user(self.owner, "bob", "Bob", "Kiwi-Lamp-456")
        self.user = self.auth.login("bob", "Kiwi-Lamp-456")

    def close(self) -> None:
        self.db.close()


@pytest.fixture
def accounts(tmp_path) -> Accounts:
    env = Accounts(tmp_path / "test.db")
    yield env
    env.close()
