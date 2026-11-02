"""Google Gemini adapter (official ``google-genai`` SDK).

* The model ID comes from configuration, so free-tier and paid models use the
  same code; only the key/model/billing on the Google side differ.
* The SDK's own automatic retries are switched off (``attempts=1``) so DUMBOO
  controls retries and never silently repeats requests.
* Optional parameters (temperature, thinking level) are only sent when set. If
  a model rejects one, it is dropped for that model and the request is sent
  again once.
* 429 errors are split into "rate limit" (per-minute; waiting helps) and
  "quota exhausted" (per-day or billing; waiting a minute does not help) using
  the quota IDs Google returns.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Iterator

from ai import errors as E
from ai.base import AIProvider, AIRequest, AIResult, CancelToken, HealthStatus, StreamChunk

log = logging.getLogger(__name__)

_FINISH = {"STOP": "stop", "MAX_TOKENS": "max_tokens", "SAFETY": "blocked", "PROHIBITED_CONTENT": "blocked",
           "BLOCKLIST": "blocked", "SPII": "blocked", "RECITATION": "blocked"}
_RETRY_DELAY = re.compile(r"retry(?:Delay'?\"?\s*[:=]\s*'?\"?| in )\s*([0-9.]+)\s*s", re.I)


def _retry_after(text: str) -> float | None:
    match = _RETRY_DELAY.search(text)
    return float(match.group(1)) if match else None


def _quota_ids(details: Any) -> list[str]:
    ids: list[str] = []
    try:
        for item in (details or {}).get("error", {}).get("details", []) or []:
            for violation in item.get("violations", []) or []:
                if violation.get("quotaId"):
                    ids.append(str(violation["quotaId"]))
    except AttributeError:
        pass
    return ids


def map_gemini_error(exc: Exception, model: str) -> E.AIError:
    """Translate a google-genai / httpx exception into an ``AIError``."""
    from google.genai import errors as gerrors

    name = type(exc).__name__
    if isinstance(exc, gerrors.APIError):
        code, status, message = exc.code or 0, (exc.status or ""), (exc.message or "")
        text = f"{status} {message} {exc.details}"
        detail = f"Gemini {code} {status}: {message[:300]}"
        low = text.lower()
        if code == 429 or status == "RESOURCE_EXHAUSTED":
            retry_after = _retry_after(str(exc.details))
            ids = _quota_ids(exc.details)
            daily = any("PerDay" in q for q in ids) or "per day" in low
            if daily or (not ids and "quota" in low and "billing" in low and "minute" not in low):
                return E.QuotaError(
                    f"Gemini quota for {model} is used up (daily limit). Options: wait for the daily reset "
                    "(midnight Pacific time), pick another model (for example a Flash-Lite model) in "
                    "SETTINGS → AI, enable billing for your Google project, or explicitly choose another provider.",
                    detail=detail, provider="gemini", retry_after=retry_after)
            return E.RateLimitError(
                f"Gemini rate limit reached for {model}. Wait {int(retry_after) + 1 if retry_after else 'a few'} "
                "seconds and try again.", detail=detail, provider="gemini", retry_after=retry_after)
        if "api key not valid" in low or "api_key_invalid" in low or code == 401:
            return E.AuthError("Gemini rejected the API key. Check GEMINI_API_KEY (create one at "
                               "https://aistudio.google.com/apikey).", detail=detail, provider="gemini")
        if code == 403:
            return E.AuthError("Gemini refused access for this key (permission denied). Make sure the "
                               "Generative Language API is enabled for the key's project.", detail=detail,
                               provider="gemini")
        if code == 404:
            return E.ModelNotFoundError(f"Gemini model '{model}' was not found or is not available to this "
                                        "account. Pick another model in SETTINGS → AI.", detail=detail,
                                        provider="gemini")
        if code == 400:
            if "thinking" in low:
                return E.UnsupportedParameterError("thinking", detail=detail, provider="gemini")
            if "temperature" in low:
                return E.UnsupportedParameterError("temperature", detail=detail, provider="gemini")
            if "location" in low or "region" in low or "country" in low:
                return E.AuthError("Gemini API is not available in your region for this account.",
                                   detail=detail, provider="gemini")
            if "token" in low and ("exceed" in low or "too long" in low or "limit" in low):
                return E.InputTooLongError(detail=detail, provider="gemini")
            return E.BadRequestError(f"Gemini rejected the request: {message[:160]}", detail=detail,
                                     provider="gemini")
        if code in (500, 502, 503, 504) or status in ("UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED"):
            return E.ProviderUnavailableError("Gemini is temporarily unavailable or overloaded. Try again shortly.",
                                              detail=detail, provider="gemini")
        return E.AIError(f"Gemini error {code}: {message[:160]}", detail=detail, provider="gemini")
    if "Timeout" in name:
        return E.TimeoutError_(detail=f"Gemini timeout: {name}", provider="gemini")
    if name in ("ConnectError", "ReadError", "WriteError", "RemoteProtocolError", "NetworkError",
                "ConnectionError", "ProxyError") or isinstance(exc, (ConnectionError, OSError)):
        return E.NetworkError(detail=f"Gemini network error: {name}", provider="gemini")
    return E.MalformedResponseError(detail=f"Gemini unexpected {name}: {str(exc)[:200]}", provider="gemini")


class GeminiProvider(AIProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str, thinking_level: str = "low", client: Any = None,
                 base_url: str | None = None) -> None:
        self.model = model
        self._api_key = api_key
        self._base_url = base_url  # only for tests against a local fake server
        self._thinking_level = (thinking_level or "").upper()
        self._client = client
        self._unsupported: set[str] = set()
        self._lock = threading.Lock()
        self._clients: dict[float, Any] = {}
        self._active = None

    # -- SDK plumbing ----------------------------------------------------------------
    def _get_client(self, timeout_s: float):
        if self._client is not None:
            return self._client
        # Keep the client alive: a genai.Client closes its HTTP connection when it is garbage-collected,
        # which would break a request or stream still in progress. One client per timeout value.
        with self._lock:
            client = self._clients.get(timeout_s)
            if client is None:
                from google import genai
                from google.genai import types

                options: dict[str, Any] = {"timeout": int(timeout_s * 1000),
                                           "retry_options": types.HttpRetryOptions(attempts=1)}
                if self._base_url:
                    options["base_url"] = self._base_url
                client = genai.Client(api_key=self._api_key, http_options=types.HttpOptions(**options))
                self._clients[timeout_s] = client
            return client

    def _config(self, request: AIRequest):
        from google.genai import types

        kwargs: dict[str, Any] = {"system_instruction": request.system_prompt,
                                  "max_output_tokens": request.max_output_tokens}
        if request.temperature is not None and "temperature" not in self._unsupported:
            kwargs["temperature"] = request.temperature
        if self._thinking_level and "thinking" not in self._unsupported:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=self._thinking_level)
        return types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _contents(request: AIRequest):
        from google.genai import types

        contents = []
        for m in request.messages:
            parts = [types.Part(text=m.content)]
            parts += [types.Part.from_bytes(data=image, mime_type="image/png") for image in m.images]
            contents.append(types.Content(role="model" if m.role == "assistant" else "user", parts=parts))
        return contents

    def _with_param_fallback(self, call):
        """Run ``call``; if the model rejects an optional parameter, drop it and try once more."""
        for _ in range(3):
            try:
                return call()
            except E.UnsupportedParameterError as exc:
                if exc.parameter in self._unsupported:
                    raise
                log.warning("Gemini model %s does not accept '%s'; retrying without it", self.model, exc.parameter)
                self._unsupported.add(exc.parameter)
        return call()

    # -- interface ------------------------------------------------------------------------
    def generate_response(self, request: AIRequest, cancel: CancelToken | None = None) -> AIResult:
        def call() -> AIResult:
            client = self._get_client(request.timeout_s)
            try:
                response = client.models.generate_content(model=self.model, contents=self._contents(request),
                                                          config=self._config(request))
            except Exception as exc:
                raise map_gemini_error(exc, self.model) from exc
            return self._result(response)

        return self._with_param_fallback(call)

    def _result(self, response) -> AIResult:
        block = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
        if block:
            raise E.BlockedError(detail=f"Gemini prompt blocked: {block}", provider="gemini")
        finish = self._finish(response)
        text = (getattr(response, "text", None) or "").strip()
        if not text:
            if finish == "blocked":
                raise E.BlockedError(provider="gemini")
            if finish == "max_tokens":
                raise E.EmptyResponseError("The model used its whole output budget before answering. Raise "
                                           "'Max output tokens' or lower the thinking level in SETTINGS → AI.",
                                           provider="gemini")
            raise E.EmptyResponseError(provider="gemini")
        return AIResult(text=text, finish_reason=finish, model=self.model)

    @staticmethod
    def _finish(response) -> str:
        try:
            reason = response.candidates[0].finish_reason
        except (AttributeError, IndexError, TypeError):
            return "stop"
        if reason is None:
            return "stop"
        value = getattr(reason, "value", str(reason))
        return _FINISH.get(value, "stop" if value in ("FINISH_REASON_UNSPECIFIED",) else "other")

    def stream_response(self, request: AIRequest, cancel: CancelToken | None = None) -> Iterator[StreamChunk]:
        started = False

        def open_stream():
            client = self._get_client(request.timeout_s)
            try:
                iterator = client.models.generate_content_stream(model=self.model, contents=self._contents(request),
                                                                 config=self._config(request))
                first = next(iterator, None)  # errors (bad key, quota…) surface on the first read
            except Exception as exc:
                raise map_gemini_error(exc, self.model) from exc
            return iterator, first

        iterator, first = self._with_param_fallback(open_stream)
        with self._lock:
            self._active = iterator
        finish = None
        try:
            chunk = first
            while chunk is not None:
                if cancel is not None and cancel.cancelled:
                    raise E.CancelledError(provider="gemini")
                block = getattr(getattr(chunk, "prompt_feedback", None), "block_reason", None)
                if block:
                    raise E.BlockedError(detail=f"Gemini prompt blocked: {block}", provider="gemini")
                text = getattr(chunk, "text", None) or ""
                if getattr(chunk, "candidates", None):
                    reason = self._finish(chunk)
                    if chunk.candidates[0].finish_reason is not None:
                        finish = reason
                if text:
                    started = True
                    yield StreamChunk(text=text)
                try:
                    chunk = next(iterator, None)
                except Exception as exc:
                    raise map_gemini_error(exc, self.model) from exc
        finally:
            with self._lock:
                self._active = None
            close = getattr(iterator, "close", None)
            if close:
                try:
                    close()
                except Exception:
                    pass
        if not started:
            if finish == "blocked":
                raise E.BlockedError(provider="gemini")
            if finish == "max_tokens":
                raise E.EmptyResponseError("The model used its whole output budget before answering. Raise "
                                           "'Max output tokens' or lower the thinking level.", provider="gemini")
            raise E.EmptyResponseError(provider="gemini")
        yield StreamChunk(finish_reason=finish or "stop")

    def health_check(self) -> HealthStatus:
        start = time.monotonic()
        try:
            info = self._get_client(15).models.get(model=self.model)
        except Exception as exc:
            err = map_gemini_error(exc, self.model)
            return HealthStatus(False, err.user_message, details={"kind": err.kind})
        latency = time.monotonic() - start
        limit = getattr(info, "output_token_limit", None)
        name = getattr(info, "display_name", None) or self.model
        return HealthStatus(True, f"Connected to Gemini · {name}" + (f" (max output {limit} tokens)" if limit else ""),
                            latency_s=latency, details={"output_token_limit": limit})

    def cancel_generation(self) -> None:
        # The google-genai stream cannot be closed from another thread while it is
        # being read; the worker checks the cancel token after every chunk and stops.
        pass
