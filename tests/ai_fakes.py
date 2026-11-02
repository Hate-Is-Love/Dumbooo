"""Fake AI provider and streaming-capable fake TTS for controller/orchestrator tests."""

from __future__ import annotations

import threading
from typing import Iterator

from ai import errors as E
from ai.base import AIProvider, AIRequest, AIResult, CancelToken, HealthStatus, StreamChunk


class ScriptedProvider(AIProvider):
    """Answers with scripted text, or raises scripted errors (one per call, in order)."""

    name = "fake"

    def __init__(self, answer: str | None = None, pieces: list[str] | None = None,
                 errors: list[E.AIError] | None = None, fail_after: int | None = None,
                 gate: threading.Event | None = None, model: str = "fake-model") -> None:
        self.model = model
        self.answer = answer
        self.pieces = pieces
        self.errors = list(errors or [])
        self.fail_after = fail_after          # raise errors[0] after this many pieces (mid-stream)
        self.gate = gate                      # if set, wait for it before the first piece
        self.requests: list[AIRequest] = []
        self.cancelled = 0

    def _text(self, request: AIRequest) -> str:
        return self.answer if self.answer is not None else f"Answer to: {request.messages[-1].content}."

    def generate_response(self, request: AIRequest, cancel: CancelToken | None = None) -> AIResult:
        self.requests.append(request)
        if self.errors:
            raise self.errors.pop(0)
        return AIResult(self._text(request), model=self.model)

    def stream_response(self, request: AIRequest, cancel: CancelToken | None = None) -> Iterator[StreamChunk]:
        self.requests.append(request)
        if self.errors and self.fail_after is None:
            raise self.errors.pop(0)
        if self.gate is not None:
            while not self.gate.wait(0.01):
                if cancel is not None and cancel.cancelled:
                    raise E.CancelledError()
        pieces = self.pieces or [w + " " for w in self._text(request).split(" ")]
        for i, piece in enumerate(pieces):
            if self.fail_after is not None and i == self.fail_after and self.errors:
                raise self.errors.pop(0)
            if cancel is not None and cancel.cancelled:
                raise E.CancelledError()
            yield StreamChunk(text=piece)
        yield StreamChunk(finish_reason="stop")

    def health_check(self) -> HealthStatus:
        return HealthStatus(True, "fake ok")

    def cancel_generation(self) -> None:
        self.cancelled += 1


class FakeTTS:
    """Mirrors TextToSpeech's public API and records what would be spoken."""

    def __init__(self, ready: bool = True) -> None:
        self.ready = ready
        self.enabled = True
        self.stops = 0
        self._id = 0
        self.streams: dict[int, list[str]] = {}
        self.ended: set[int] = set()

    @property
    def available(self) -> bool:
        return self.ready and self.enabled

    def begin_stream(self):
        if not self.available:
            return None
        self._id += 1
        self.streams[self._id] = []
        return self._id

    def feed(self, utterance_id, text):
        self.streams[utterance_id].append(text)

    def end_stream(self, utterance_id):
        self.ended.add(utterance_id)

    def speak(self, text):
        uid = self.begin_stream()
        if uid is not None:
            self.feed(uid, text)
            self.end_stream(uid)
        return uid

    @property
    def spoken(self) -> list[str]:
        return [" ".join(parts) for parts in self.streams.values()]

    def stop_speaking(self):
        self.stops += 1
