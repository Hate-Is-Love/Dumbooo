"""OpenAI adapter (official ``openai`` SDK, Responses API).

* ``store=False``: conversations are not stored on OpenAI's side for later
  retrieval through the API.
* The SDK's automatic retries are disabled (``max_retries=0``); DUMBOO
  decides when a limited retry is appropriate.
* ``temperature`` and ``reasoning.effort`` are optional. Reasoning models
  reject ``temperature`` and older models reject ``reasoning``; a rejected
  parameter is dropped for that model and the request is sent once more.
* HTTP 429 with code ``insufficient_quota`` means the API account has no
  credit (a ChatGPT subscription does not include API credit). It is reported
  as a quota problem, not retried.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Iterator

from ai import errors as E
from ai.base import AIProvider, AIRequest, AIResult, CancelToken, HealthStatus, StreamChunk

log = logging.getLogger(__name__)


def _retry_after(exc: Any) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    try:
        if headers.get("retry-after-ms"):
            return float(headers["retry-after-ms"]) / 1000
        if headers.get("retry-after"):
            return float(headers["retry-after"])
    except (TypeError, ValueError):
        return None
    return None


def _body_error(exc: Any) -> dict:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        return body.get("error", body) if isinstance(body.get("error", body), dict) else {}
    return {}


# Wording per service that speaks the OpenAI protocol (OpenAI itself, Groq).
SERVICES = {
    "openai": {"label": "OpenAI", "key": "OPENAI_API_KEY", "keys_url": "https://platform.openai.com/api-keys",
               "no_credit": ("Your OpenAI API account has no remaining credit or quota. Add billing at "
                             "https://platform.openai.com/settings/organization/billing — a ChatGPT "
                             "subscription does not include API credit.")},
    "groq": {"label": "Groq", "key": "GROQ_API_KEY", "keys_url": "https://console.groq.com/keys",
             "no_credit": "Your Groq account's quota is used up (see https://console.groq.com/settings/limits)."},
}


def map_openai_error(exc: Exception, model: str, provider: str = "openai") -> E.AIError:
    import openai

    service = SERVICES[provider]
    label = service["label"]
    err = _body_error(exc)
    code = str(err.get("code") or "")
    etype = str(err.get("type") or "")
    message = str(err.get("message") or getattr(exc, "message", "") or "")
    param = str(err.get("param") or "")
    detail = f"{label} {type(exc).__name__} code={code} type={etype} param={param}: {message[:300]}"
    low = message.lower()

    if isinstance(exc, openai.APITimeoutError):
        return E.TimeoutError_(detail=detail, provider=provider)
    if isinstance(exc, openai.APIConnectionError):
        return E.NetworkError(detail=detail, provider=provider)
    if isinstance(exc, openai.AuthenticationError):
        return E.AuthError(f"{label} rejected the API key. Check {service['key']} "
                           f"({service['keys_url']}).", detail=detail, provider=provider)
    if isinstance(exc, openai.PermissionDeniedError):
        return E.AuthError(f"{label} refused access for this key or region (permission denied).",
                           detail=detail, provider=provider)
    if isinstance(exc, openai.NotFoundError):
        return E.ModelNotFoundError(f"{label} model '{model}' was not found or this account cannot use it. "
                                    "Pick another model in SETTINGS → AI.", detail=detail, provider=provider)
    if isinstance(exc, openai.RateLimitError):
        if code == "insufficient_quota" or etype == "insufficient_quota":
            return E.QuotaError(service["no_credit"], detail=detail, provider=provider)
        retry_after = _retry_after(exc)
        return E.RateLimitError(f"{label} rate limit reached for {model}. Wait a moment and try again.",
                                detail=detail, provider=provider, retry_after=retry_after)
    if isinstance(exc, openai.BadRequestError):
        if param == "temperature" or ("temperature" in low and "support" in low):
            return E.UnsupportedParameterError("temperature", detail=detail, provider=provider)
        if param.startswith("reasoning") or ("reasoning" in low and "support" in low):
            return E.UnsupportedParameterError("reasoning", detail=detail, provider=provider)
        if code == "context_length_exceeded" or "context window" in low or "maximum context" in low:
            return E.InputTooLongError(detail=detail, provider=provider)
        if code == "model_not_found" or "does not exist" in low:
            return E.ModelNotFoundError(f"{label} model '{model}' is not available to this account.",
                                        detail=detail, provider=provider)
        return E.BadRequestError(f"{label} rejected the request: {message[:160]}", detail=detail, provider=provider)
    if isinstance(exc, openai.InternalServerError) or (
            isinstance(exc, openai.APIStatusError) and getattr(exc, "status_code", 0) >= 500):
        return E.ProviderUnavailableError(f"{label} is temporarily unavailable. Try again shortly.",
                                          detail=detail, provider=provider)
    if isinstance(exc, openai.APIStatusError):
        return E.AIError(f"{label} error {exc.status_code}: {message[:160]}", detail=detail, provider=provider)
    return E.MalformedResponseError(detail=f"{label} unexpected {type(exc).__name__}: {str(exc)[:200]}",
                                    provider=provider)


def _event_error(event: Any) -> E.AIError:
    err = getattr(event, "error", None) or getattr(getattr(event, "response", None), "error", None)
    code = str(getattr(err, "code", "") or "")
    message = str(getattr(err, "message", "") or "stream failed")
    if code in ("rate_limit_exceeded",):
        return E.RateLimitError(detail=f"OpenAI stream {code}: {message}", provider="openai")
    if code in ("insufficient_quota",):
        return E.QuotaError(detail=f"OpenAI stream {code}: {message}", provider="openai")
    if code in ("server_error", "overloaded"):
        return E.ProviderUnavailableError(detail=f"OpenAI stream {code}: {message}", provider="openai")
    return E.AIError(f"OpenAI stopped the answer: {message[:160]}", detail=f"OpenAI stream {code}: {message}",
                     provider="openai")


class OpenAIProvider(AIProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str, reasoning_effort: str = "low", client: Any = None,
                 base_url: str | None = None) -> None:
        self.model = model
        self._api_key = api_key
        self._effort = (reasoning_effort or "").lower()
        self._client = client
        self._base_url = base_url
        self._unsupported: set[str] = set()
        self._lock = threading.Lock()
        self._clients: dict[float, Any] = {}
        self._active_stream = None

    def _get_client(self, timeout_s: float):
        if self._client is not None:
            return self._client
        with self._lock:  # one long-lived client per timeout (connection reuse; never closed mid-stream)
            client = self._clients.get(timeout_s)
            if client is None:
                import openai

                kwargs: dict[str, Any] = {"api_key": self._api_key, "timeout": timeout_s, "max_retries": 0}
                if self._base_url:
                    kwargs["base_url"] = self._base_url
                client = openai.OpenAI(**kwargs)
                self._clients[timeout_s] = client
            return client

    @staticmethod
    def _message(m) -> dict[str, Any]:
        if not m.images:
            return {"role": m.role, "content": m.content}
        import base64

        content: list[dict[str, Any]] = [{"type": "input_text", "text": m.content}]
        content += [{"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(i).decode("ascii")}
                    for i in m.images]
        return {"role": m.role, "content": content}

    def _params(self, request: AIRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "instructions": request.system_prompt,
            "input": [self._message(m) for m in request.messages],
            "max_output_tokens": request.max_output_tokens,
            "store": False,
        }
        if request.temperature is not None and "temperature" not in self._unsupported:
            params["temperature"] = request.temperature
        if self._effort and "reasoning" not in self._unsupported:
            params["reasoning"] = {"effort": self._effort}
        return params

    def _with_param_fallback(self, call):
        for _ in range(3):
            try:
                return call()
            except E.UnsupportedParameterError as exc:
                if exc.parameter in self._unsupported:
                    raise
                log.warning("OpenAI model %s does not accept '%s'; retrying without it", self.model, exc.parameter)
                self._unsupported.add(exc.parameter)
        return call()

    @staticmethod
    def _finish(response: Any) -> str:
        status = getattr(response, "status", None)
        if status == "incomplete":
            reason = getattr(getattr(response, "incomplete_details", None), "reason", "")
            return "max_tokens" if reason == "max_output_tokens" else ("blocked" if reason == "content_filter" else "other")
        return "stop"

    def generate_response(self, request: AIRequest, cancel: CancelToken | None = None) -> AIResult:
        def call():
            try:
                return self._get_client(request.timeout_s).responses.create(**self._params(request))
            except Exception as exc:
                raise map_openai_error(exc, self.model) from exc

        response = self._with_param_fallback(call)
        if getattr(response, "status", None) == "failed":
            raise _event_error(response)
        finish = self._finish(response)
        text = (getattr(response, "output_text", "") or "").strip()
        if not text:
            if finish == "max_tokens":
                raise E.EmptyResponseError("The model used its whole output budget (reasoning) before answering. "
                                           "Raise 'Max output tokens' or lower the reasoning effort.", provider="openai")
            if finish == "blocked":
                raise E.BlockedError(provider="openai")
            raise E.EmptyResponseError(provider="openai")
        return AIResult(text=text, finish_reason=finish, model=self.model)

    def stream_response(self, request: AIRequest, cancel: CancelToken | None = None) -> Iterator[StreamChunk]:
        def open_stream():
            try:
                stream = self._get_client(request.timeout_s).responses.create(**self._params(request), stream=True)
                iterator = iter(stream)
                first = next(iterator, None)  # HTTP errors surface here
            except Exception as exc:
                raise map_openai_error(exc, self.model) from exc
            return stream, iterator, first

        stream, iterator, first = self._with_param_fallback(open_stream)
        with self._lock:
            self._active_stream = stream
        started, finish = False, None
        try:
            event = first
            while event is not None:
                if cancel is not None and cancel.cancelled:
                    raise E.CancelledError(provider="openai")
                etype = getattr(event, "type", "")
                if etype == "response.output_text.delta":
                    delta = getattr(event, "delta", "") or ""
                    if delta:
                        started = True
                        yield StreamChunk(text=delta)
                elif etype == "response.completed":
                    finish = "stop"
                elif etype == "response.incomplete":
                    finish = self._finish(getattr(event, "response", None))
                elif etype in ("response.failed", "error"):
                    raise _event_error(event)
                try:
                    event = next(iterator, None)
                except Exception as exc:
                    if cancel is not None and cancel.cancelled:
                        raise E.CancelledError(provider="openai") from exc
                    raise map_openai_error(exc, self.model) from exc
        finally:
            with self._lock:
                self._active_stream = None
            try:
                stream.close()
            except Exception:
                pass
        if not started:
            if finish == "max_tokens":
                raise E.EmptyResponseError("The model used its whole output budget (reasoning) before answering. "
                                           "Raise 'Max output tokens' or lower the reasoning effort.", provider="openai")
            if finish == "blocked":
                raise E.BlockedError(provider="openai")
            raise E.EmptyResponseError(provider="openai")
        yield StreamChunk(finish_reason=finish or "stop")

    def health_check(self) -> HealthStatus:
        start = time.monotonic()
        try:
            info = self._get_client(15).models.retrieve(self.model)
        except Exception as exc:
            err = map_openai_error(exc, self.model)
            return HealthStatus(False, err.user_message, details={"kind": err.kind})
        return HealthStatus(True, f"Connected to OpenAI · {getattr(info, 'id', self.model)} is available to this key",
                            latency_s=time.monotonic() - start)

    def cancel_generation(self) -> None:
        # Closing the HTTP stream from here makes the reading thread stop at once.
        with self._lock:
            stream = self._active_stream
        if stream is not None:
            try:
                stream.close()
            except Exception:
                log.debug("Could not close OpenAI stream", exc_info=True)
