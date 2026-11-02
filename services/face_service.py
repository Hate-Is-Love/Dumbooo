"""Face unlock accounts: setting up, removing and matching faces, with lockout.

* **Setup** needs a signed-in user *and* their password again. Only SFace feature vectors are
  stored, encrypted with Windows DPAPI for the current Windows user (purpose "face"), never
  pictures. A face that already unlocks another account is refused.
* **Sign-in** compares the camera's feature vector with every stored face; the best match must
  pass ``security.face.MATCH`` over several frames (see ``FaceSignIn``).
* **Lockout:** after ``MAX_FAILURES`` failed face sign-ins within ``LOCK_WINDOW``, face unlock is
  paused until someone signs in with a password (or the window has passed).
* Policy ``accounts.face_unlock_enabled`` (owner, ADMIN page) switches face unlock off for everyone.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Callable

import numpy as np

from security.face import ENROLL_SAMPLES, LEARNED_MAX, MATCH, MODEL_NAME, cosine
from services.auth_service import AuthError, AuthService, Session

log = logging.getLogger(__name__)

MAX_FAILURES = 5
LOCK_WINDOW = timedelta(minutes=15)


class FaceError(AuthError):
    user_message = "Face unlock is not available."


class FaceService:
    name = "face"

    def __init__(self, db, auth: AuthService, policy: Callable[[], dict],
                 protect: Callable[[bytes], bytes] | None = None, unprotect: Callable[[bytes], bytes] | None = None,
                 clock: Callable[[], datetime] = datetime.now) -> None:
        if protect is None or unprotect is None:
            from security import token_store

            protect = protect or (lambda b: token_store.protect(b, "face-v1"))
            unprotect = unprotect or (lambda b: token_store.unprotect(b, "face-v1"))
        self._db = db
        self._auth = auth
        self._policy = policy
        self._protect = protect
        self._unprotect = unprotect
        self._clock = clock
        self._lock = threading.Lock()

    def _iso(self, when: datetime | None = None) -> str:
        return (when or self._clock()).isoformat(timespec="seconds")

    @property
    def enabled(self) -> bool:
        return bool(self._policy().get("face_unlock_enabled", True))

    # -- setup -------------------------------------------------------------------------------------
    def status(self, session: Session) -> dict:
        user = self._auth.require(session)
        row = self._db.row("SELECT samples, updated_at FROM face_profiles WHERE user_id = ?", (user.id,))
        return {"enrolled": row is not None, "samples": row["samples"] if row else 0,
                "updated_at": row["updated_at"] if row else None, "enabled": self.enabled}

    def enroll(self, session: Session, password: str, embeddings: np.ndarray) -> int:
        if not self.enabled:
            raise FaceError("Face unlock is switched off by the owner.")
        self._auth.reauthenticate(session, password)  # proves it's really the account holder
        user = self._auth.require(session)
        vectors = np.asarray(embeddings, dtype=np.float32)
        if vectors.ndim != 2 or vectors.shape[0] < 4 or vectors.shape[1] != 128:
            raise FaceError("Not enough face samples were captured. Please try again.")
        mean = vectors.mean(axis=0)
        for other_id, other in self._profiles(exclude=user.id):
            if max(cosine(mean, v) for v in other) >= MATCH:
                log.warning("Face setup refused: matches another account")
                raise FaceError("This face is already set up for another account.")
        blob = self._protect(vectors.tobytes())
        now = self._iso()
        self._db.execute("INSERT INTO face_profiles (user_id, embeddings, samples, model, created_at, updated_at) "
                         "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET embeddings = excluded.embeddings, "
                         "samples = excluded.samples, model = excluded.model, updated_at = excluded.updated_at",
                         (user.id, blob, int(vectors.shape[0]), MODEL_NAME, now, now))
        log.info("Face unlock set up (%d samples)", vectors.shape[0])
        return int(vectors.shape[0])

    def remove(self, session: Session) -> bool:
        user = self._auth.require(session)
        removed = self._db.execute("DELETE FROM face_profiles WHERE user_id = ?", (user.id,))
        log.info("Face unlock data removed")
        return bool(removed)

    # -- sign-in ------------------------------------------------------------------------------------
    def any_enrolled(self) -> bool:
        return self.enabled and self._db.row("SELECT 1 AS x FROM face_profiles LIMIT 1") is not None

    def _profiles(self, exclude: int | None = None) -> list[tuple[int, np.ndarray]]:
        out = []
        for row in self._db.query("SELECT f.user_id, f.embeddings FROM face_profiles f JOIN users u ON u.id = f.user_id "
                                  "WHERE u.status = 'active'"):
            if row["user_id"] == exclude:
                continue
            try:
                vectors = np.frombuffer(self._unprotect(bytes(row["embeddings"])), dtype=np.float32).reshape(-1, 128)
            except Exception:
                log.warning("A stored face could not be decrypted (another Windows user or a copied database)")
                continue
            out.append((row["user_id"], vectors))
        return out

    def matcher(self) -> Callable[[np.ndarray], tuple[int | None, float]]:
        """A fast in-memory matcher for one sign-in attempt (decrypts once)."""
        if self.locked():
            raise FaceError("Face unlock is paused after too many failed attempts. Sign in with your password.")
        profiles = self._profiles()

        def identify(embedding: np.ndarray) -> tuple[int | None, float]:
            best_user, best = None, -1.0
            for user_id, vectors in profiles:
                score = float(np.max(vectors @ embedding))  # vectors and embedding are unit length
                if score > best:
                    best_user, best = user_id, score
            return best_user, best

        return identify

    def locked(self) -> bool:
        since = (self._clock() - LOCK_WINDOW).isoformat(timespec="seconds")
        failures = self._db.row("SELECT COUNT(*) AS n FROM face_attempts WHERE success = 0 AND attempted_at > ?",
                                (since,))["n"]
        return failures >= MAX_FAILURES

    def clear_failures(self) -> None:
        """A successful password sign-in lifts a face-unlock pause."""
        self._db.execute("DELETE FROM face_attempts WHERE success = 0")

    def record_failure(self, reason: str) -> None:
        self._db.execute("INSERT INTO face_attempts (user_id, success, reason, attempted_at) VALUES (NULL, 0, ?, ?)",
                         (reason[:80], self._iso()))

    def sign_in(self, user_id: int, samples=()) -> Session:
        """Called after ``FaceSignIn`` confirmed this user (recognised + head turn)."""
        if not self.enabled:
            raise FaceError("Face unlock is switched off by the owner.")
        if self.locked():
            raise FaceError("Face unlock is paused after too many failed attempts. Sign in with your password.")
        if self._db.row("SELECT 1 AS x FROM face_profiles WHERE user_id = ?", (int(user_id),)) is None:
            raise FaceError()
        session = self._auth.login_verified(user_id, "face")
        self._db.execute("INSERT INTO face_attempts (user_id, success, reason, attempted_at) VALUES (?, 1, '', ?)",
                         (int(user_id), self._iso()))
        try:
            self._learn(int(user_id), samples)
        except Exception:
            log.warning("Could not update the face profile after sign-in", exc_info=True)
        return session

    def _learn(self, user_id: int, samples) -> int:
        """Keep a few frames of a confirmed sign-in so the profile covers more lighting conditions.

        The threshold is unchanged. A frame is only kept if it matches this user's *original* setup
        samples at ``MATCH`` (so the profile can't drift towards another face), doesn't match any other
        account, and adds something new (not a near-copy). Setup samples are never replaced; at most
        ``LEARNED_MAX`` learned ones are kept, the oldest dropped first.
        """
        new = [np.asarray(s, dtype=np.float32).reshape(128) for s in samples or ()]
        if not new:
            return 0
        row = self._db.row("SELECT embeddings FROM face_profiles WHERE user_id = ?", (user_id,))
        if row is None:
            return 0
        vectors = np.frombuffer(self._unprotect(bytes(row["embeddings"])), dtype=np.float32).reshape(-1, 128)
        originals, learned = vectors[:ENROLL_SAMPLES], list(vectors[ENROLL_SAMPLES:])
        others = self._profiles(exclude=user_id)
        added = 0
        for v in new:
            v = v / (np.linalg.norm(v) or 1.0)
            if float(np.max(originals @ v)) < MATCH:
                continue                                   # not confidently the enrolled person
            if any(float(np.max(o @ v)) >= MATCH for _uid, o in others):
                continue                                   # looks like someone else too: never learn it
            if float(np.max(np.vstack([originals] + learned) @ v)) >= 0.92:
                continue                                   # nothing new
            learned.append(v)
            added += 1
        if not added:
            return 0
        learned = learned[-LEARNED_MAX:]
        merged = np.vstack([originals] + learned).astype(np.float32)
        self._db.execute("UPDATE face_profiles SET embeddings = ?, samples = ?, updated_at = ? WHERE user_id = ?",
                         (self._protect(merged.tobytes()), int(merged.shape[0]), self._iso(), user_id))
        log.info("Face profile updated from a confirmed sign-in (%d setup + %d learned samples)",
                 len(originals), len(learned))
        return added
