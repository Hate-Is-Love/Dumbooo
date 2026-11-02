"""Groq adapter (OpenAI-compatible Chat Completions API, through the official ``openai`` SDK).

Groq runs open models (``openai/gpt-oss-120b``, ``qwen/…``) on its own LPU hardware: the
first words usually arrive in well under a second, which keeps a spoken conversation
natural. Measured on this project's laptop: 0.3–0.8 s to the first words.

* The SDK's automatic retries are disabled (``max_retries=0``); DUMBOO decides.
* ``reasoning_effort`` keeps reasoning models quick ("low"). The models' hidden
  reasoning is never shown or spoken (``include_reasoning=False``). A model that
  rejects a parameter gets the request again without it.
* Groq's chat models here don't accept images; a screenshot question fails with a
  "bad request", and the switching layer (``ai/fallback.py``) hands it to Gemini.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Iterator

from ai import errors as E
from ai.base import AIProvider, AIRequest, AIResult, CancelToken, HealthStatus, StreamChunk
from ai.providers.openai_provider import map_openai_error

log = logging.getLogger(__name__)

BASE_URL = "https://api.groq.com/openai/v1"
_FINISH = {"stop": "stop", "length": "max_tokens", "content_filter": "blocked"}


class GroqProvider(AIProvider):
    name = "groq"

    def __init__(self, api_key: str, model: str, reasoning_effort: str = "low", client: Any = None) -> None:
        self.model = model
        self._api_key = api_key
        self._effort = (reasoning_effort or "").lower()
        self._client = client
        self._unsupported: set[str] = set()
        self._lock = threading.Lock()
        self._clients: dict[float, Any] = {}
        self._active_stream = None

    def _get_client(self, timeout_s: float):
        if self._client is not None:
            return self._client
        with self._lock:  # one connection pool for every timeout, so warm_up() helps the first answer
            if "base" not in self._clients:
                import openai

                self._clients["base"] = openai.OpenAI(api_key=self._api_key, base_url=BASE_URL, max_retries=0)
            return self._clients["base"].with_options(timeout=timeout_s)

    def warm_up(self) -> None:
        """Open the HTTPS connection now (a model listing: free, nothing generated)."""
        try:
            self._get_client(10).models.list()
        except Exception:
            log.debug("Groq warm-up failed", exc_info=True)

    @staticmethod
    def _message(m) -> dict[str, Any]:
        if not m.images:
            return {"role": m.role, "content": m.content}
        import base64

        content: list[dict[str, Any]] = [{"type": "text", "text": m.content}]
        content += [{"type": "image_url", "image_url": {"url": "data:image/png;base64,"
                                                               + base64.b64encode(i).decode("ascii")}}
                    for i in m.images]
        return {"role": m.role, "content": content}

    def _params(self, request: AIRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": request.system_prompt}]
                        + [self._message(m) for m in request.messages],
            "max_completion_tokens": request.max_output_tokens,
        }
        if request.temperature is not None and "temperature" not in self._unsupported:
            params["temperature"] = request.temperature
        if self._effort and "reasoning" not in self._unsupported:
            params["reasoning_effort"] = self._effort
        if "include_reasoning" not in self._unsupported:
            # Groq-only option: the OpenAI SDK rejects unknown arguments, so it travels in extra_body.
            params["extra_body"] = {"include_reasoning": False}
        return params

    def _with_param_fallback(self, call):
        for _ in range(4):
            try:
                return call()
            except E.BadRequestError as exc:
                bad = next((p for p in ("include_reasoning", "reasoning_effort", "temperature")
                            if p in (exc.detail or "") and p not in self._unsupported), None)
                if bad is None:
                    raise
                self._unsupported.add("reasoning" if bad == "reasoning_effort" else bad)
                log.warning("Groq model %s does not accept '%s'; retrying without it", self.model, bad)
            except E.UnsupportedParameterError as exc:
                if exc.parameter in self._unsupported:
                    raise
                self._unsupported.add(exc.parameter)
                log.warning("Groq model %s does not accept '%s'; retrying without it", self.model, exc.parameter)
        return call()

    def _create(self, request: AIRequest, **extra):
        try:
            return self._get_client(request.timeout_s).chat.completions.create(**self._params(request), **extra)
        except Exception as exc:
            raise map_openai_error(exc, self.model, "groq") from exc

    def generate_response(self, request: AIRequest, cancel: CancelToken | None = None) -> AIResult:
        response = self._with_param_fallback(lambda: self._create(request))
        choice = (getattr(response, "choices", None) or [None])[0]
        if choice is None:
            raise E.MalformedResponseError(provider="groq")
        finish = _FINISH.get(getattr(choice, "finish_reason", "") or "stop", "other")
        text = (getattr(choice.message, "content", "") or "").strip()
        if not text:
            if finish == "blocked":
                raise E.BlockedError(provider="groq")
            raise E.EmptyResponseError(provider="groq")
        return AIResult(text=text, finish_reason=finish, model=self.model)

    def stream_response(self, request: AIRequest, cancel: CancelToken | None = None) -> Iterator[StreamChunk]:
        def open_stream():
            stream = self._create(request, stream=True)
            iterator = iter(stream)
            try:
                first = next(iterator, None)  # HTTP errors surface here
            except Exception as exc:
                raise map_openai_error(exc, self.model, "groq") from exc
            return stream, iterator, first

        stream, iterator, first = self._with_param_fallback(open_stream)
        with self._lock:
            self._active_stream = stream
        started, finish = False, None
        try:
            chunk = first
            while chunk is not None:
                if cancel is not None and cancel.cancelled:
                    raise E.CancelledError(provider="groq")
                for choice in getattr(chunk, "choices", None) or []:
                    delta = getattr(getattr(choice, "delta", None), "content", None) or ""
                    if delta:
                        started = True
                        yield StreamChunk(text=delta)
                    if getattr(choice, "finish_reason", None):
                        finish = _FINISH.get(choice.finish_reason, "other")
                try:
                    chunk = next(iterator, None)
                except Exception as exc:
                    if cancel is not None and cancel.cancelled:
                        raise E.CancelledError(provider="groq") from exc
                    raise map_openai_error(exc, self.model, "groq") from exc
        finally:
            with self._lock:
                self._active_stream = None
            try:
                stream.close()
            except Exception:
                pass
        if not started:
            if finish == "blocked":
                raise E.BlockedError(provider="groq")
            raise E.EmptyResponseError(provider="groq")
        yield StreamChunk(finish_reason=finish or "stop")

    def health_check(self) -> HealthStatus:
        start = time.monotonic()
        try:
            info = self._get_client(15).models.retrieve(self.model)
        except Exception as exc:
            err = map_openai_error(exc, self.model, "groq")
            return HealthStatus(False, err.user_message, details={"kind": err.kind})
        return HealthStatus(True, f"Connected to Groq · {getattr(info, 'id', self.model)} is available to this key",
                            latency_s=time.monotonic() - start)

    def cancel_generation(self) -> None:
        with self._lock:
            stream = self._active_stream
        if stream is not None:
            try:
                stream.close()
            except Exception:
                log.debug("Could not close Groq stream", exc_info=True)
