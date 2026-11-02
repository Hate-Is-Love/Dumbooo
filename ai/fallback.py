"""Automatic switching between AI providers and keys.

``FallbackProvider`` looks like one ``AIProvider`` to the rest of DUMBOO. Inside it
holds an ordered list of *routes* (provider + model + key), for example::

    Groq key 1 → Groq key 2 → Gemini → OpenAI

Each request goes to the first route that is not resting. If that route fails
**before any text was shown**, the next one is tried at once, and the failed route
rests for a while (``COOLDOWN_S``) so later messages don't wait for it again:

* quota used up, key rejected, model unavailable: a long rest (the problem won't fix
  itself in seconds);
* rate limit: the provider's own retry hint;
* network, timeout, overload, empty or unreadable answer: a short rest;
* a request this model can't take (e.g. a screenshot for a text-only model): no rest,
  just the next route for this request.

Never switched: a safety-filter block (the answer was refused, not broken) and
cancellation. Once text has started to stream, a failure is reported instead of
silently starting the answer again elsewhere.

Routes other than the last available one get a shorter timeout (``switch_timeout_s``)
so a slow provider doesn't keep the user waiting when another is ready.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Iterator

from ai import errors as E
from ai.base import AIProvider, AIRequest, AIResult, CancelToken, HealthStatus, StreamChunk

log = logging.getLogger(__name__)

LABELS = {"groq": "Groq", "gemini": "Gemini", "openai": "OpenAI", "echo": "Echo"}
# error kind -> seconds the route rests (None: skip only for this request)
COOLDOWN_S: dict[str, float | None] = {
    "quota": 1800, "auth": 3600, "config": 3600, "model": 3600,
    "rate_limit": 20, "network": 15, "timeout": 30, "unavailable": 30,
    "empty": 10, "malformed": 10, "error": 10,
    "bad_request": None, "unsupported_parameter": None, "too_long": None,
}
NEVER_SWITCH = {"blocked", "cancelled"}
MAX_RATE_LIMIT_REST_S = 1800.0


@dataclass
class Route:
    provider: str
    model: str
    key: str
    index: int                 # 1 = the provider's main key, 2 = GROQ_API_KEY_2 …
    resting_until: float = 0.0
    last_error: str = ""
    instance: AIProvider | None = None

    @property
    def label(self) -> str:
        return f"{LABELS.get(self.provider, self.provider)}" + (f" (key {self.index})" if self.index > 1 else "")


class FallbackProvider(AIProvider):
    """Tries routes in order; see the module docstring."""

    def __init__(self, routes: list[tuple[str, str, str]], build: Callable[[str, str, str], AIProvider],
                 switch_timeout_s: float = 12.0, clock: Callable[[], float] = time.monotonic) -> None:
        if not routes:
            raise E.ConfigError("No AI provider has an API key. Add one in SETTINGS → AI.")
        counts: dict[str, int] = {}
        self.routes: list[Route] = []
        for provider, model, key in routes:
            counts[provider] = counts.get(provider, 0) + 1
            self.routes.append(Route(provider, model, key, counts[provider]))
        self._build = build
        self._switch_timeout = switch_timeout_s
        self._clock = clock
        self._lock = threading.Lock()
        self._current: Route | None = None
        self.on_switch: Callable[[str, str, str], None] | None = None  # (from label, to label, reason)
        self.name, self.model = self.routes[0].provider, self.routes[0].model

    # -- route bookkeeping ---------------------------------------------------------------
    def _instance(self, route: Route) -> AIProvider:
        if route.instance is None:
            route.instance = self._build(route.provider, route.model, route.key)
        return route.instance

    def _order(self) -> list[Route]:
        """Routes to try now: the ready ones in order, then resting ones (soonest first) as a last resort."""
        now = self._clock()
        ready = [r for r in self.routes if r.resting_until <= now]
        resting = sorted((r for r in self.routes if r.resting_until > now), key=lambda r: r.resting_until)
        return ready + resting

    def _rest(self, route: Route, exc: E.AIError) -> None:
        seconds = COOLDOWN_S.get(exc.kind, 10)
        if exc.kind == "rate_limit" and exc.retry_after:
            seconds = min(MAX_RATE_LIMIT_REST_S, max(float(exc.retry_after), 5.0))
        route.last_error = exc.kind
        if seconds:
            route.resting_until = self._clock() + seconds
        log.warning("AI route %s failed (%s): %s", route.label, exc.kind, exc.detail or exc.user_message)

    def _succeeded(self, route: Route) -> None:
        route.resting_until, route.last_error = 0.0, ""
        self.name, self.model = route.provider, route.model

    @staticmethod
    def _should_switch(exc: E.AIError) -> bool:
        return exc.kind not in NEVER_SWITCH

    def _notify(self, failed: Route, nxt: Route, exc: E.AIError) -> None:
        log.info("Switching AI from %s to %s (%s)", failed.label, nxt.label, exc.kind)
        if self.on_switch is not None:
            try:
                self.on_switch(failed.label, nxt.label, exc.kind)
            except Exception:
                log.debug("on_switch listener failed", exc_info=True)

    def _request_for(self, request: AIRequest, position: int, total: int) -> AIRequest:
        if position < total - 1 and request.timeout_s > self._switch_timeout:
            return replace(request, timeout_s=self._switch_timeout)
        return request

    # -- AIProvider ------------------------------------------------------------------------------
    def generate_response(self, request: AIRequest, cancel: CancelToken | None = None) -> AIResult:
        order = self._order()
        last: E.AIError | None = None
        for i, route in enumerate(order):
            if cancel is not None and cancel.cancelled:
                raise E.CancelledError()
            if last is not None:
                self._notify(order[i - 1], route, last)
            try:
                provider = self._instance(route)
                with self._lock:
                    self._current = route
                result = provider.generate_response(self._request_for(request, i, len(order)), cancel)
            except E.AIError as exc:
                if not self._should_switch(exc) or (cancel is not None and cancel.cancelled):
                    raise
                self._rest(route, exc)
                last = exc
                continue
            finally:
                with self._lock:
                    self._current = None
            self._succeeded(route)
            return result
        assert last is not None
        raise last

    def stream_response(self, request: AIRequest, cancel: CancelToken | None = None) -> Iterator[StreamChunk]:
        order = self._order()
        last: E.AIError | None = None
        for i, route in enumerate(order):
            if cancel is not None and cancel.cancelled:
                raise E.CancelledError()
            if last is not None:
                self._notify(order[i - 1], route, last)
            started = False
            try:
                provider = self._instance(route)
                with self._lock:
                    self._current = route
                sub = self._request_for(request, i, len(order))
                if provider.supports_streaming:
                    for chunk in provider.stream_response(sub, cancel):
                        if chunk.text and not started:
                            started = True
                            self._succeeded(route)
                        yield chunk
                else:
                    result = provider.generate_response(sub, cancel)
                    started = True
                    self._succeeded(route)
                    yield StreamChunk(text=result.text)
                    yield StreamChunk(finish_reason=result.finish_reason)
                if not started:  # a stream that ended without text
                    raise E.EmptyResponseError(provider=route.provider)
                return
            except E.AIError as exc:
                if started or not self._should_switch(exc) or (cancel is not None and cancel.cancelled):
                    raise
                self._rest(route, exc)
                last = exc
            finally:
                with self._lock:
                    self._current = None
        assert last is not None
        raise last

    def health_check(self) -> HealthStatus:
        lines, ok_any, start = [], False, time.monotonic()
        for route in self.routes:
            try:
                status = self._instance(route).health_check()
            except E.AIError as exc:
                status = HealthStatus(False, exc.user_message)
            ok_any = ok_any or status.ok
            lines.append(f"{'✓' if status.ok else '✗'} {route.label} · {route.model}: "
                         f"{'ready' if status.ok else status.message}")
        return HealthStatus(ok_any, "\n".join(lines), latency_s=time.monotonic() - start,
                            details={"routes": len(self.routes)})

    def warm_up(self) -> None:
        order = self._order()
        if order:
            self._instance(order[0]).warm_up()

    def cancel_generation(self) -> None:
        with self._lock:
            route = self._current
        if route is not None and route.instance is not None:
            route.instance.cancel_generation()

    @property
    def supports_streaming(self) -> bool:
        return True

    def describe(self) -> str:
        return " → ".join(f"{r.label} · {r.model}" for r in self.routes)
