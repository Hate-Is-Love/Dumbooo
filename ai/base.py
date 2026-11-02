"""The provider-independent AI interface.

Every provider adapter implements ``AIProvider``. Calls are synchronous and
run on the conversation controller's worker thread, never on the UI thread.
(Synchronous SDK calls on a worker thread fit DUMBOO's existing threading
model; an asyncio event loop would add complexity without making the dashboard
any more responsive.)
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator, Literal

Role = Literal["user", "assistant"]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str
    images: tuple[bytes, ...] = ()       # PNG screenshots (Module 4 screen understanding), user messages only


@dataclass
class AIRequest:
    system_prompt: str
    messages: list[ChatMessage]          # oldest first; the last one is the new user message
    max_output_tokens: int = 1024
    temperature: float | None = None     # None = model default (always supported)
    timeout_s: float = 30.0


@dataclass
class AIResult:
    text: str
    finish_reason: str = "stop"          # "stop" | "max_tokens" | "blocked" | "other"
    model: str = ""

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "max_tokens"


@dataclass
class StreamChunk:
    text: str = ""
    finish_reason: str | None = None     # set on the final chunk when known


@dataclass
class HealthStatus:
    ok: bool
    message: str
    latency_s: float | None = None
    details: dict = field(default_factory=dict)


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, seconds: float) -> bool:
        """Sleep up to ``seconds``; returns True early if cancelled."""
        return self._event.wait(seconds)


class AIProvider(ABC):
    name: str = "base"
    model: str = ""

    @abstractmethod
    def generate_response(self, request: AIRequest, cancel: CancelToken | None = None) -> AIResult:
        """Return the complete answer. Raises ``ai.errors.AIError`` subclasses."""

    @abstractmethod
    def stream_response(self, request: AIRequest, cancel: CancelToken | None = None) -> Iterator[StreamChunk]:
        """Yield the answer in pieces as the provider produces them."""

    @abstractmethod
    def health_check(self) -> HealthStatus:
        """Check the key and model access without generating text (no generation quota used)."""

    def cancel_generation(self) -> None:
        """Abort the in-flight request if the SDK allows it. Safe to call any time."""

    def warm_up(self) -> None:
        """Open the connection ahead of the first message (no text generated). Optional."""

    @property
    def supports_streaming(self) -> bool:
        return True

    def describe(self) -> str:
        return f"{self.name} · {self.model}"
