"""Who is signed in to this DUMBOO right now.

Holds the single active desktop ``Session`` and coordinates what must happen
when it changes:

* sign-in: start a fresh conversation context for that user, publish
  ``USER_SIGNED_IN`` (the dashboard unlocks and shows their name/title);
* sign-out, inactivity timeout, or the account being disabled: cancel any
  answer in progress, stop speaking, clear the in-memory conversation context
  and per-session caches, revoke the session, publish ``USER_SIGNED_OUT``
  (the dashboard locks and clears private views).

"Stay signed in on this PC" stores the session token encrypted with Windows
DPAPI (see ``security/token_store.py``); at start-up it is validated like any
other session and discarded if expired or revoked.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from app.event_bus import EventBus, Events
from security.token_store import TokenStore
from services.auth_service import AuthError, AuthService, Session
from services.profile_service import ProfileService

log = logging.getLogger(__name__)

CHECK_INTERVAL_S = 10.0


class AccountManager:
    name = "accounts"

    def __init__(self, bus: EventBus, auth: AuthService, profile: ProfileService, token_store: TokenStore | None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._bus = bus
        self.auth = auth
        self.profile = profile
        self._store = token_store
        self._mono = monotonic
        self._session: Session | None = None
        self._lock = threading.RLock()
        self._cleanup: list[Callable[[Session | None], None]] = []
        self._next_check = 0.0
        self._last_touch = 0.0
        self.face = None  # services.face_service.FaceService, set by the application

    # -- wiring ------------------------------------------------------------------------
    def on_sign_out(self, callback: Callable[[Session | None], None]) -> None:
        """Register cleanup to run (with the old session) whenever the user changes or signs out."""
        self._cleanup.append(callback)

    def start(self) -> None:
        self._unsubscribe = [self._bus.subscribe(Events.TICK, lambda _n, _p: self.check()),
                             self._bus.subscribe(Events.USER_TEXT, lambda _n, _p: self.touch()),
                             self._bus.subscribe(Events.UI_ACTIVITY, lambda _n, _p: self.touch())]
        self.restore()

    def stop(self) -> None:
        for unsubscribe in getattr(self, "_unsubscribe", []):
            unsubscribe()

    # -- state ---------------------------------------------------------------------------
    @property
    def session(self) -> Session | None:
        with self._lock:
            return self._session

    @property
    def signed_in(self) -> bool:
        return self.session is not None

    def current_user(self):
        session = self.session
        if session is None:
            return None
        try:
            return self.auth.require(session)
        except AuthError:
            return None

    def needs_owner_setup(self) -> bool:
        return self.auth.needs_owner_setup()

    def remember_me_available(self) -> bool:
        from security import token_store

        return token_store.available() and self._store is not None

    # -- sign in / out ----------------------------------------------------------------------
    def setup_owner(self, username: str, display_name: str, password: str, title: str | None = None) -> Session:
        self.auth.setup_owner(username, display_name, password, title)
        return self.login(username, password)

    def login(self, username: str, password: str, remember: bool = False) -> Session:
        session = self.auth.login(username, password, remember=remember and self.remember_me_available())
        if self.face is not None:
            self.face.clear_failures()  # a password sign-in lifts a face-unlock pause
        self._activate(session, method="password")
        if session.persistent and self._store is not None:
            self._store.save(session.token)
        return session

    def restore(self) -> bool:
        """Resume a remembered session at start-up, if one exists and is still valid."""
        if self._store is None:
            return False
        token = self._store.load()
        if not token:
            return False
        try:
            session = self.auth.resume(token)
        except AuthError:
            self._store.clear()
            log.info("Remembered session is no longer valid; sign-in required")
            return False
        self._activate(session, method="remembered")
        log.info("Remembered session restored")
        return True

    def login_with_face(self, user_id: int, samples=()) -> Session:
        """After the camera check (recognised + head turn) confirmed ``user_id``. See services/face_service.py."""
        if self.face is None:
            raise AuthError("Face unlock is not available.")
        session = self.face.sign_in(user_id, samples)
        self._activate(session, method="face")
        return session

    def _activate(self, session: Session, method: str = "password") -> None:
        with self._lock:
            previous, self._session = self._session, session
        if previous is not None:
            self._run_cleanup(previous)
            self.auth.logout(previous, "switched user")
        self._run_cleanup(None)  # fresh in-memory context for the new user
        self._last_touch = self._mono()
        self._next_check = self._mono() + CHECK_INTERVAL_S
        user = self.auth.require(session)
        prefs = self.profile.get(session)
        self._bus.publish(Events.USER_SIGNED_IN, {
            "username": user.username, "display_name": user.display_name, "role": user.role,
            "title": prefs["title"], "must_change_password": user.must_change_password,
            "permissions": sorted(user.permissions), "persistent": session.persistent, "method": method,
        })

    def logout(self, reason: str = "signed out") -> None:
        with self._lock:
            session, self._session = self._session, None
        if session is None:
            return
        self._run_cleanup(session)
        self.auth.logout(session, reason)
        if self._store is not None:
            self._store.clear()  # a signed-out session is revoked; never keep its token around
        log.info("Signed out (%s)", reason)
        self._bus.publish(Events.USER_SIGNED_OUT, {"reason": reason})

    def _run_cleanup(self, session: Session | None) -> None:
        for callback in self._cleanup:
            try:
                callback(session)
            except Exception:
                log.exception("Sign-out cleanup step failed")

    # -- activity and expiry -------------------------------------------------------------------
    def touch(self) -> None:
        session = self.session
        if session is None:
            return
        now = self._mono()
        if now - self._last_touch > 15:
            self._last_touch = now
            try:
                self.auth.touch(session)
            except Exception:
                log.debug("Session touch failed", exc_info=True)

    def check(self) -> None:
        """Periodic validation (every ~10 s): inactivity timeout, revoked or disabled account."""
        session = self.session
        if session is None or self._mono() < self._next_check:
            return
        self._next_check = self._mono() + CHECK_INTERVAL_S
        try:
            self.auth.require(session)
        except AuthError as exc:
            log.info("Session ended: %s", exc.user_message)
            with self._lock:
                if self._session is not session:
                    return
            self.logout(exc.user_message)

    def refresh_profile(self) -> None:
        session = self.session
        if session is not None:
            self._bus.publish(Events.PROFILE_CHANGED, self.profile.get(session))
