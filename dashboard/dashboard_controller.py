"""Bridge between the (Qt-free) assistant core and the Qt dashboard.

* ``QtEventBridge`` re-emits every event-bus event as a Qt signal. Qt queues
  cross-thread signals, so slots always run on the UI thread.
* ``DashboardController`` exposes the actions the UI may trigger and runs
  database work on a ``QThreadPool`` so the window never waits on disk I/O.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from app.application import AssistantCore
from app.event_bus import Events
from services.auth_service import NotAuthenticated

log = logging.getLogger(__name__)


class QtEventBridge(QObject):
    event = Signal(str, dict)

    def __init__(self, core: AssistantCore) -> None:
        super().__init__()
        self._unsubscribe = core.bus.subscribe("*", self._forward)

    def _forward(self, name: str, payload: dict) -> None:
        if name == Events.TICK:
            return
        # Never hand raw audio or session objects (tokens) to the UI thread.
        if "audio" in payload or "scope" in payload:
            payload = {k: v for k, v in payload.items() if k not in ("audio", "scope")}
        self.event.emit(name, payload)

    def close(self) -> None:
        self._unsubscribe()


def friendly_message(exc: BaseException) -> str:
    """Short, safe text for the dashboard. Internal details stay in the log."""
    message = getattr(exc, "user_message", None)
    if message:
        return message
    if isinstance(exc, (ValueError, KeyError, LookupError)):
        text = str(exc).strip("'\"")
        return text or "That item was not found."
    log.error("Unexpected error in a background job: %s", exc.__class__.__name__)
    return "Something went wrong. Details are in the log file."


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(object)


class _Job(QRunnable):
    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as exc:
            log.debug("Background job failed", exc_info=True)
            self.signals.failed.emit(exc)
        else:
            self.signals.done.emit(result)


class DashboardController(QObject):
    def __init__(self, core: AssistantCore) -> None:
        super().__init__()
        self.core = core
        self.bridge = QtEventBridge(core)
        self._pool = QThreadPool.globalInstance()
        self._jobs: set[_Job] = set()

    # -- background work ------------------------------------------------------------------
    def run_async(self, fn: Callable[[], Any], on_done: Callable[[Any], None] | None = None,
                  on_error: Callable[[str], None] | None = None,
                  on_exception: Callable[[BaseException], None] | None = None) -> None:
        """Run ``fn`` on a worker thread; callbacks run on the UI thread.

        ``on_error`` receives a friendly message; ``on_exception`` (if given) the exception itself,
        e.g. to ask for the password again on ``ReauthRequired``.
        """
        job = _Job(fn)
        job.setAutoDelete(False)
        self._jobs.add(job)

        def finished_ok(result: Any) -> None:
            self._jobs.discard(job)
            if on_done:
                on_done(result)

        def finished_err(exc: BaseException) -> None:
            self._jobs.discard(job)
            if isinstance(exc, NotAuthenticated) and self.core.accounts.signed_in:
                self.core.accounts.check()  # the session ended: lock the dashboard
            if on_exception:
                on_exception(exc)
            elif on_error:
                on_error(friendly_message(exc))

        job.signals.done.connect(finished_ok)
        job.signals.failed.connect(finished_err)
        self._pool.start(job)

    def track(self, indicator, label: str, fn: Callable[[], Any], on_done: Callable[[Any], None] | None = None,
              on_error: Callable[[str], None] | None = None, done_label: str | None = None,
              on_exception: Callable[[BaseException], None] | None = None) -> None:
        """``run_async`` plus a live HUD animation on ``indicator`` for exactly as long as the work runs."""
        indicator.begin(label)

        def ok(result: Any) -> None:
            indicator.end(True, done_label)
            if on_done:
                on_done(result)

        def failed(exc: BaseException) -> None:
            indicator.end(False, "FAILED")
            if on_exception:
                on_exception(exc)
            elif on_error:
                on_error(friendly_message(exc))

        self.run_async(fn, ok, on_exception=failed)

    # -- accounts -----------------------------------------------------------------------------
    @property
    def session(self):
        """The signed-in user's session (passed to every service call; never exposed in widgets' text)."""
        return self.core.accounts.session

    def logout(self) -> None:
        self.core.accounts.logout("signed out")

    def continue_conversation(self, conversation: dict) -> None:
        """Make an earlier conversation current and give DUMBOO its recent turns back as context."""
        session = self.session
        self.core.recorder.continue_conversation(session, conversation["id"])  # verifies ownership
        context = self.core.conversation.context
        context.clear()
        pending_user = None
        for m in conversation["messages"]:
            if m["role"] == "user":
                pending_user = m
            elif m["role"] == "assistant" and m["status"] == "completed" and pending_user is not None:
                context.add_turn(pending_user["content"], m["content"],
                                 "voice" if pending_user["input_mode"] == "voice" else "typed")
                pending_user = None

    def update_system_settings(self, changes: dict) -> list[str]:
        return self.core.auth.update_system_settings(self.session, self.core.settings, changes)

    # -- assistant actions --------------------------------------------------------------------
    def send_text(self, text: str) -> bool:
        text = text.strip()
        if not text:
            return False
        if len(text) > 2000:
            text = text[:2000]
        self.core.bus.publish(Events.USER_TEXT, {"text": text, "source": "typed"})
        return True

    def keep_awake(self) -> None:
        """Something is going on in the dashboard (e.g. a face scan): don't fall asleep. Throttled to every 2 s."""
        import time

        now = time.monotonic()
        if now - getattr(self, "_awake_at", 0.0) > 2.0:
            self._awake_at = now
            self.core.bus.publish(Events.UI_ACTIVITY, {})

    def talk(self) -> None:
        self.core.bus.publish(Events.LISTEN_REQUESTED, {"source": "ui"})

    def stop_speaking(self) -> None:
        """STOP button: cancels the answer being generated and any speech."""
        self.core.bus.publish(Events.STOP_SPEAKING, {"source": "ui"})

    def retry(self) -> None:
        self.core.bus.publish(Events.RETRY_REQUESTED, {})

    def clear_conversation(self) -> None:
        self.core.bus.publish(Events.CONVERSATION_CLEARED, {})

    def save_api_key(self, provider: str, key: str, extra: bool = False) -> str:
        """Store a key in the local .env file (git-ignored). Returns the masked result for display.

        ``extra=True`` keeps the existing key(s) and adds this one in the next free numbered
        variable (``GROQ_API_KEY_2`` …), used when the others hit a limit.
        """
        from ai.config import api_keys_for, key_variables
        from utils.env import mask, set_env_value

        key = key.strip()
        if not key or any(ch.isspace() for ch in key):
            raise ValueError("An API key cannot be empty or contain spaces.")
        variables = key_variables(provider)
        variable = variables[0]
        if extra:
            if key in api_keys_for(provider, self.core.environ):
                raise ValueError("This key is already saved.")
            free = [v for v in variables[1:] if not (self.core.environ.get(v) or "").strip()
                    and v != "GOOGLE_API_KEY"]
            if not (self.core.environ.get(variables[0]) or "").strip():
                free = [variables[0]]
            if not free:
                raise ValueError("All key slots are used. Replace a key instead.")
            variable = free[0]
        set_env_value(self.core.paths.root / ".env", variable, key, self.core.environ)
        self.core.reload_ai_keys()
        return mask(key)

    def test_ai_connection(self, on_done) -> None:
        """Check key + model access in the background (no text is generated)."""
        from ai.base import HealthStatus

        self.run_async(self.core.conversation.health_check, on_done,
                       lambda message: on_done(HealthStatus(False, f"Check failed: {message}")))

    def sleep(self, source: str = "ui") -> None:
        self.core.bus.publish(Events.SLEEP_REQUESTED, {"source": source})

    def wake(self, source: str = "ui") -> None:
        self.core.bus.publish(Events.WAKE_REQUESTED, {"source": source})

    def set_mic_muted(self, muted: bool) -> None:
        self.core.mic.muted = muted
        self.core.bus.publish(Events.MIC_STATUS, {"open": self.core.pipeline.mic_ok,
                                                  "device": self.core.mic.device_name or "System default",
                                                  "error": None, "muted": muted})

    def shutdown(self) -> None:
        self.bridge.close()
        self._pool.waitForDone(2000)
        self.core.shutdown()
