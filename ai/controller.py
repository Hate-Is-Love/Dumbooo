"""The conversation controller: one place where every message (typed or spoken) is answered.

Request policy (deliberately simple and predictable):

* One answer is generated at a time. A **new message cancels the answer in
  progress** ("latest message wins"); the cancelled answer is discarded and not
  stored in the context.
* The **same text from the same source within 3 seconds is ignored** (double
  clicks, a transcript arriving twice).
* Each request carries the orchestrator's sequence number (``seq``). Every event
  the controller publishes carries it too, and anything that belongs to an older
  ``seq`` is ignored downstream, so an old answer can never overwrite a newer one.
* Automatic retries happen only for temporary problems (network, timeout,
  per-minute rate limit, provider overload), at most ``max_retries`` times,
  with backoff that respects the provider's retry hint, and only before any
  text has been shown. Authentication and quota errors are never retried.
* Switching to another provider or key (when ``auto_switch`` is on and more than
  one has a key) happens inside the provider (``ai/fallback.py``); the controller
  only announces it (``AI_SWITCHED``).

Events published: ``AI_STARTED``, ``AI_RETRYING``, ``AI_SWITCHED``, ``RESPONSE_DELTA``,
``RESPONSE_READY``, ``RESPONSE_FAILED``, ``RESPONSE_CANCELLED``, ``AI_STATUS``.

Module 3 hooks (``ConversationHooks``) let the account/memory layer, without
the controller knowing about databases:

* ``local_reply``: answer locally before any AI call (sign-in required, memory
  commands such as "remember …" / "forget …");
* ``prompt_options``: per-user style (concise or detailed);
* ``extra_context``: authorised, relevant saved memories and past messages.

Every request carries an opaque ``scope`` (the signed-in user's Session) that is
passed to the hooks and included in AI events for the history recorder.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ai import errors as E
from ai.base import AIProvider, AIRequest, CancelToken, HealthStatus
from ai.config import AIConfig
from ai.context import ConversationContext, shorten_user_message
from ai.factory import create_provider
from ai.prompts import build_system_prompt, load_base_prompt
from ai.speech_format import to_speech
from app.event_bus import EventBus, Events

log = logging.getLogger(__name__)

DUPLICATE_WINDOW_S = 3.0


@dataclass
class LocalReply:
    """An answer produced without the AI provider (memory commands, sign-in prompts)."""

    text: str
    speech: str | None = None
    kind: str = "local"
    memory_ids: list[int] = field(default_factory=list)
    listen_after: bool = False   # a question (e.g. "shall I do it?"): voice users can answer without the wake word


class ConversationHooks:
    """No-op defaults; Module 3's ``MemoryAssistant`` overrides these."""

    def local_reply(self, scope: Any, text: str, source: str) -> LocalReply | None:
        return None

    def prompt_options(self, scope: Any) -> dict:
        return {}

    def extra_context(self, scope: Any, text: str, seq: int) -> str:
        return ""
MAX_RETRY_WAIT_S = 20.0


@dataclass
class _Turn:
    seq: int
    text: str
    source: str
    started_at: float
    scope: Any = None
    retry_of: int | None = None
    cancel: CancelToken = field(default_factory=CancelToken)
    parts: list[str] = field(default_factory=list)


class ConversationController:
    name = "conversation"

    def __init__(self, bus: EventBus, config: AIConfig, root: Path,
                 provider_factory: Callable[[AIConfig], AIProvider] = create_provider,
                 voice_output: Callable[[], bool] = lambda: True, run_inline: bool = False,
                 clock=time.monotonic, hooks: ConversationHooks | None = None) -> None:
        self._bus = bus
        self.hooks = hooks or ConversationHooks()
        self._root = root
        self._factory = provider_factory
        self._voice_output = voice_output
        self._inline = run_inline
        self._clock = clock
        self._lock = threading.RLock()
        self._config = config
        self._provider: AIProvider | None = None
        self._answered_by: tuple[str, str] | None = None  # (provider, model) of the last answer
        self.context = ConversationContext(config.context_turns, config.context_max_chars)
        self._active: _Turn | None = None
        self._last_accepted: tuple[str, str, float] | None = None
        self._last_failed: tuple[str, str, Any, int] | None = None
        self._threads: list[threading.Thread] = []
        self.last_error: E.AIError | None = None

    # -- lifecycle -----------------------------------------------------------------
    def start(self) -> None:
        self.publish_status()
        if not self._inline:
            threading.Thread(target=self._warm_up, name="AI-warmup", daemon=True).start()

    def _warm_up(self) -> None:
        """Connect to the provider before the first message, so the first answer is as quick as the rest."""
        try:
            self._get_provider().warm_up()
        except Exception:
            log.debug("AI warm-up skipped", exc_info=True)

    def stop(self) -> None:
        self.cancel("shutdown")
        for thread in list(self._threads):
            thread.join(timeout=0.5)

    @property
    def config(self) -> AIConfig:
        with self._lock:
            return self._config

    def reconfigure(self, config: AIConfig) -> None:
        """Apply new settings (provider, model, limits). The context is kept."""
        with self._lock:
            self._config = config
            self._provider = None  # rebuilt on the next request
            self._answered_by = None
            self.context.configure(config.context_turns, config.context_max_chars)
        self.publish_status()

    def publish_status(self) -> None:
        config = self.config
        try:
            config.validate()
            ready, detail = True, config.describe()
        except E.ConfigError as exc:
            ready, detail = False, exc.user_message
        provider, model = self._answered_by or (config.provider, config.model)
        self._bus.publish(Events.AI_STATUS, {"ready": ready, "provider": provider, "model": model,
                                             "detail": detail, "turns": self.context.turn_count})

    def _get_provider(self) -> AIProvider:
        with self._lock:
            if self._provider is None:
                self._provider = self._factory(self._config)
                if hasattr(self._provider, "on_switch"):
                    self._provider.on_switch = self._on_switch
                log.info("AI provider ready: %s", self._config.describe())
            return self._provider

    def _on_switch(self, failed: str, to: str, kind: str) -> None:
        reasons = {"quota": "quota used up", "rate_limit": "busy (rate limit)", "auth": "key rejected",
                   "model": "model unavailable", "network": "unreachable", "timeout": "too slow",
                   "unavailable": "temporarily down", "bad_request": "can't handle this request"}
        with self._lock:
            seq = self._active.seq if self._active is not None else None
        self._bus.publish(Events.AI_SWITCHED, {"seq": seq, "from": failed, "to": to,
                                               "reason": reasons.get(kind, "had a problem")})

    # -- state ---------------------------------------------------------------------------
    @property
    def busy(self) -> bool:
        with self._lock:
            return self._active is not None

    @property
    def can_retry(self) -> bool:
        return self._last_failed is not None

    # -- requests ----------------------------------------------------------------------------
    def _is_duplicate(self, key: str, source: str, now: float) -> bool:
        last = self._last_accepted
        return last is not None and last[0] == key and last[1] == source and now - last[2] < DUPLICATE_WINDOW_S

    def accepts(self, text: str, source: str) -> bool:
        """Would ``submit`` take this message (not empty, not a duplicate)?"""
        text = (text or "").strip()
        with self._lock:
            return bool(text) and not self._is_duplicate(" ".join(text.lower().split()), source, self._clock())

    def submit(self, text: str, source: str, seq: int, *, allow_duplicate: bool = False, scope: Any = None,
               retry_of: int | None = None) -> bool:
        """Start answering ``text``. Returns False if the message was empty or a duplicate."""
        text = (text or "").strip()
        if not text:
            return False
        now = self._clock()
        key = " ".join(text.lower().split())
        with self._lock:
            if not allow_duplicate and self._is_duplicate(key, source, now):
                log.info("Ignoring duplicate %s message", source)
                return False
            self._last_accepted = (key, source, now)
            previous = self._active
            turn = _Turn(seq, text, source, started_at=now, scope=scope, retry_of=retry_of)
            self._active = turn
        if previous is not None:
            self._cancel_turn(previous, "superseded by a newer message")
        config = self.config
        self._bus.publish(Events.AI_STARTED, {"seq": seq, "text": text, "source": source,
                                              "provider": config.provider, "model": config.model,
                                              "scope": scope, "retry_of": retry_of})
        if self._inline:
            self._run(turn)
        else:
            thread = threading.Thread(target=self._run, args=(turn,), name=f"AI-{seq}", daemon=True)
            self._threads = [t for t in self._threads if t.is_alive()] + [thread]
            thread.start()
        return True

    def retry(self, seq: int) -> bool:
        """Send the last failed message again (the user pressed RETRY)."""
        failed = self._last_failed
        if failed is None:
            return False
        text, source, scope, failed_seq = failed
        return self.submit(text, source, seq, allow_duplicate=True, scope=scope, retry_of=failed_seq)

    def cancel(self, reason: str = "cancelled") -> bool:
        with self._lock:
            turn, self._active = self._active, None
        if turn is None:
            return False
        self._cancel_turn(turn, reason)
        return True

    def _cancel_turn(self, turn: _Turn, reason: str) -> None:
        turn.cancel.cancel()
        provider = self._provider
        if provider is not None:
            try:
                provider.cancel_generation()
            except Exception:
                log.debug("cancel_generation failed", exc_info=True)
        log.info("AI request %s cancelled (%s)", turn.seq, reason)
        self._bus.publish(Events.RESPONSE_CANCELLED, {"seq": turn.seq, "reason": reason, "scope": turn.scope,
                                                      "partial": "".join(turn.parts)})

    def clear(self) -> None:
        self.cancel("conversation cleared")
        self.context.clear()
        self._last_failed = None
        self.publish_status()

    def health_check(self) -> HealthStatus:
        """Check key and model access (no text generation). Call from a worker thread."""
        try:
            provider = self._factory(self.config)
        except E.AIError as exc:
            return HealthStatus(False, exc.user_message)
        return provider.health_check()

    def generate_once(self, system_prompt: str, text: str, *, images: tuple[bytes, ...] = (),
                      max_output_tokens: int | None = None, cancel: CancelToken | None = None) -> str:
        """One stand-alone request (no conversation context, nothing recorded).

        Used by automation for planning and for explaining screens and command output.
        The same provider, model and key as the conversation are used. Raises ``AIError``.
        """
        from ai.base import ChatMessage

        config = self.config
        provider = self._get_provider()
        request = AIRequest(system_prompt=system_prompt, messages=[ChatMessage("user", text, tuple(images))],
                            max_output_tokens=max_output_tokens or config.max_output_tokens,
                            temperature=config.temperature, timeout_s=config.timeout_s)
        cancel = cancel or CancelToken()
        attempt = 0
        while True:
            try:
                result = provider.generate_response(request, cancel)
                if not result.text.strip():
                    raise E.EmptyResponseError(provider=provider.name)
                return result.text.strip()
            except E.AIError as exc:
                wait = self._retry_delay(exc, attempt, config, started=False)
                if wait is None or cancel.cancelled:
                    raise
                attempt += 1
                if cancel.wait(wait):
                    raise E.CancelledError() from exc

    # -- worker ------------------------------------------------------------------------------------
    def _is_current(self, turn: _Turn) -> bool:
        with self._lock:
            return self._active is turn and not turn.cancel.cancelled

    def _finish(self, turn: _Turn) -> None:
        with self._lock:
            if self._active is turn:
                self._active = None

    def _build_request(self, turn: _Turn, config: AIConfig) -> tuple[AIRequest, str]:
        user_text, shortened = shorten_user_message(turn.text)
        if shortened:
            log.info("Long message shortened before sending")
        options = self.hooks.prompt_options(turn.scope) or {}
        prompt = build_system_prompt(load_base_prompt(config.system_prompt_path(self._root)),
                                     concise=options.get("concise", config.concise),
                                     source=turn.source, voice_output=self._voice_output())
        extra = self.hooks.extra_context(turn.scope, user_text, turn.seq)
        if extra:
            prompt += "\n\n" + extra
        request = AIRequest(system_prompt=prompt, messages=self.context.messages_for(user_text),
                            max_output_tokens=config.max_output_tokens, temperature=config.temperature,
                            timeout_s=config.timeout_s)
        return request, user_text

    def _run(self, turn: _Turn) -> None:
        config = self.config
        try:
            local = self.hooks.local_reply(turn.scope, turn.text, turn.source)
        except Exception:
            log.exception("Local reply hook failed")
            local = None
        if local is not None:
            self._answer_locally(turn, local)
            return
        try:
            provider = self._get_provider()
            request, user_text = self._build_request(turn, config)
            text, finish = self._generate(provider, request, turn, config)
        except E.CancelledError:
            return
        except E.AIError as exc:
            self._fail(turn, exc)
            return
        except Exception as exc:  # a bug, not a provider error: report without a stack trace in the UI
            log.exception("Unexpected error while generating a response")
            self._fail(turn, E.AIError("Something went wrong while generating the answer.", detail=repr(exc)))
            return
        finally:
            if turn.cancel.cancelled:
                self._finish(turn)
        if not self._is_current(turn):
            return  # cancelled or superseded while the last chunk was arriving
        self.context.add_turn(user_text, text, turn.source)
        self._last_failed = None
        self.last_error = None
        self._answered_by = (provider.name, provider.model)
        self._finish(turn)
        elapsed = self._clock() - turn.started_at
        log.info("AI answer %s: %d chars in %.2fs (%s)", turn.seq, len(text), elapsed, finish)
        self._bus.publish(Events.RESPONSE_READY, {
            "seq": turn.seq, "text": text, "speech": to_speech(text), "provider": provider.name,
            "model": provider.model, "finish_reason": finish, "truncated": finish == "max_tokens",
            "elapsed": round(elapsed, 2), "streamed": config.streaming and provider.supports_streaming,
            "scope": turn.scope, "local": False,
        })
        self.publish_status()

    def _answer_locally(self, turn: _Turn, reply: LocalReply) -> None:
        if not self._is_current(turn):
            return
        self.context.add_turn(turn.text, reply.text, turn.source)
        self._last_failed = None
        self._finish(turn)
        self._bus.publish(Events.RESPONSE_READY, {
            "seq": turn.seq, "text": reply.text, "speech": reply.speech or to_speech(reply.text),
            "provider": "dumboo", "model": reply.kind, "finish_reason": "stop", "truncated": False,
            "elapsed": round(self._clock() - turn.started_at, 2), "streamed": False, "scope": turn.scope,
            "local": True, "memory_ids": list(reply.memory_ids), "listen_after": reply.listen_after,
            "source": turn.source,
        })

    def _generate(self, provider: AIProvider, request: AIRequest, turn: _Turn, config: AIConfig) -> tuple[str, str]:
        attempt = 0
        while True:
            parts = turn.parts
            parts.clear()
            try:
                if config.streaming and provider.supports_streaming:
                    finish = "stop"
                    for chunk in provider.stream_response(request, turn.cancel):
                        if not self._is_current(turn):
                            raise E.CancelledError()
                        if chunk.text:
                            parts.append(chunk.text)
                            self._bus.publish(Events.RESPONSE_DELTA, {"seq": turn.seq, "delta": chunk.text,
                                                                      "text": "".join(parts)})
                        if chunk.finish_reason:
                            finish = chunk.finish_reason
                    text = "".join(parts).strip()
                    if not text:
                        raise E.EmptyResponseError(provider=provider.name)
                    return text, finish
                result = provider.generate_response(request, turn.cancel)
                return result.text, result.finish_reason
            except E.CancelledError:
                raise
            except E.AIError as exc:
                if turn.cancel.cancelled:
                    raise E.CancelledError() from exc
                exc.partial = "".join(parts)  # type: ignore[attr-defined]
                wait = self._retry_delay(exc, attempt, config, started=bool(parts))
                if wait is None:
                    raise
                attempt += 1
                log.warning("AI request %s failed (%s); retry %d in %.1fs", turn.seq, exc.kind, attempt, wait)
                self._bus.publish(Events.AI_RETRYING, {"seq": turn.seq, "attempt": attempt, "wait_s": round(wait, 1),
                                                       "reason": exc.user_message})
                if turn.cancel.wait(wait):
                    raise E.CancelledError() from exc

    @staticmethod
    def _retry_delay(exc: E.AIError, attempt: int, config: AIConfig, started: bool) -> float | None:
        if started or not exc.retryable or attempt >= config.max_retries:
            return None
        delay = exc.retry_after if exc.retry_after is not None else 1.0 * (2 ** attempt)
        if delay > MAX_RETRY_WAIT_S:
            return None  # a long wait is the user's decision, not an automatic one
        return max(0.2, delay)

    def _fail(self, turn: _Turn, exc: E.AIError) -> None:
        if not self._is_current(turn):
            return
        self._finish(turn)
        self._last_failed = (turn.text, turn.source, turn.scope, turn.seq)
        self.last_error = exc
        log.error("AI request %s failed: %s | %s", turn.seq, exc.kind, exc.detail or exc.user_message)
        self._bus.publish(Events.RESPONSE_FAILED, {
            "seq": turn.seq, "error": exc.user_message, "kind": exc.kind, "retryable": True,
            "partial": getattr(exc, "partial", ""), "provider": exc.provider or self.config.provider,
            "scope": turn.scope,
        })
