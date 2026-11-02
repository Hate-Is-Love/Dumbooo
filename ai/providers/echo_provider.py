"""Offline test provider: repeats the message. Never used unless explicitly selected."""

from __future__ import annotations

import time
from typing import Iterator

from ai.base import AIProvider, AIRequest, AIResult, CancelToken, HealthStatus, StreamChunk


class EchoProvider(AIProvider):
    name = "echo"
    model = "echo"

    def __init__(self, delay_s: float = 0.03) -> None:
        self._delay = delay_s

    def _answer(self, request: AIRequest) -> str:
        return f"[Test mode — no AI connected] You said: {request.messages[-1].content}"

    def generate_response(self, request: AIRequest, cancel: CancelToken | None = None) -> AIResult:
        return AIResult(self._answer(request), model=self.model)

    def stream_response(self, request: AIRequest, cancel: CancelToken | None = None) -> Iterator[StreamChunk]:
        for word in self._answer(request).split(" "):
            if cancel is not None and cancel.wait(self._delay):
                return
            yield StreamChunk(text=word + " ")
        yield StreamChunk(finish_reason="stop")

    def health_check(self) -> HealthStatus:
        return HealthStatus(True, "Echo test mode: no AI provider is contacted.", latency_s=0.0)
