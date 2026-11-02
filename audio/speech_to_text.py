"""Local speech-to-text with faster-whisper.

Models are downloaded once from Hugging Face into ``models/whisper`` on first
use (``scripts/download_models.py`` does this ahead of time). A model is loaded
only once per (name, device, compute type) by ``WhisperModelPool`` and shared
between the STT service and the wake-phrase spotter.

Transcription runs on a dedicated worker thread, never on the UI thread.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from app.event_bus import EventBus, Events
from audio import SAMPLE_RATE

log = logging.getLogger(__name__)


def cpu_threads() -> int:
    """Threads for Whisper on the CPU: faster-whisper uses 4 by default; up to 8 on bigger CPUs (measured
    ~17 % faster on a 22-thread laptop, identical text), leaving cores for the UI and the wake word."""
    return max(4, min(8, (os.cpu_count() or 4) // 2))


class WhisperModelPool:
    def __init__(self, download_root: Path, loader: Callable[..., Any] | None = None) -> None:
        self._root = download_root
        self._loader = loader
        self._models: dict[tuple, Any] = {}
        self._lock = threading.Lock()
        self.load_count = 0

    def get(self, name: str, device: str = "cpu", compute_type: str = "int8") -> Any:
        key = (name, device, compute_type)
        with self._lock:
            if key not in self._models:
                loader = self._loader
                if loader is None:
                    from faster_whisper import WhisperModel as loader  # heavy import, deferred
                log.info("Loading Whisper model %s (%s, %s)", name, device, compute_type)
                self._root.mkdir(parents=True, exist_ok=True)
                threads = cpu_threads()
                try:  # offline first: no network request when the model is already downloaded
                    model = loader(name, device=device, compute_type=compute_type,
                                   download_root=str(self._root), local_files_only=True, cpu_threads=threads)
                except Exception:
                    log.info("Whisper model %s not found locally; downloading", name)
                    model = loader(name, device=device, compute_type=compute_type, download_root=str(self._root),
                                   cpu_threads=threads)
                self._models[key] = model
                self.load_count += 1
            return self._models[key]

    def release(self) -> None:
        with self._lock:
            self._models.clear()


@dataclass
class TranscriptResult:
    text: str
    language: str
    duration: float
    confidence: float  # mean token probability, 0..1

    @property
    def empty(self) -> bool:
        return not self.text


# Whisper is known to "hallucinate" these on near-silent audio.
_HALLUCINATIONS = {"thank you.", "thanks for watching!", "thank you for watching.", "you", ".", "bye."}


def transcribe_array(model: Any, audio: np.ndarray, language: str | None, *, beam_size: int = 3,
                     initial_prompt: str | None = None) -> TranscriptResult:
    audio = np.asarray(audio, dtype=np.float32)
    duration = len(audio) / SAMPLE_RATE
    if duration < 0.1:
        return TranscriptResult("", language or "", duration, 0.0)
    # without_timestamps: DUMBOO only needs the words; measured 30-40 % faster with the same words (only
    # punctuation/case differed). temperature: at most two retries on unclear audio instead of five, so a
    # mumbled clip can't take several seconds.
    segments, info = model.transcribe(
        audio, language=language or None, beam_size=beam_size, vad_filter=False,
        condition_on_previous_text=False, initial_prompt=initial_prompt,
        without_timestamps=True, temperature=(0.0, 0.2, 0.4),
    )
    texts, logprobs = [], []
    for seg in segments:
        if seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0:
            continue
        texts.append(seg.text.strip())
        logprobs.append(seg.avg_logprob)
    text = " ".join(t for t in texts if t).strip()
    if text.lower() in _HALLUCINATIONS:
        text = ""
    confidence = float(np.exp(np.mean(logprobs))) if logprobs else 0.0
    return TranscriptResult(text, getattr(info, "language", language or ""), duration, confidence)


class SpeechToText:
    name = "stt"

    def __init__(self, bus: EventBus, pool: WhisperModelPool, settings: dict) -> None:
        self._bus = bus
        self._pool = pool
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self.configure(settings)
        self.ready = False
        self.detail = "not started"

    def configure(self, settings: dict) -> None:
        self.enabled = bool(settings["enabled"])
        self.model_name = settings["model"]
        self.language = settings["language"]
        self.device = settings["device"]
        self.compute_type = settings["compute_type"]

    def _status(self, ready: bool, detail: str) -> None:
        self.ready, self.detail = ready, detail
        self._bus.publish(Events.STT_STATUS, {"ready": ready, "detail": detail})

    def start(self) -> None:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="STT")
        if not self.enabled:
            self._status(False, "disabled in settings")
            return
        self._executor.submit(self._preload)

    def _preload(self) -> None:
        self._status(False, f"loading {self.model_name}…")
        try:
            self._pool.get(self.model_name, self.device, self.compute_type)
            self._status(True, f"{self.model_name} ready ({self.device}/{self.compute_type})")
        except Exception as exc:
            log.exception("STT model failed to load")
            self._status(False, f"model load failed: {exc.__class__.__name__}: {exc}")

    def reload(self) -> None:
        if self._executor is not None and self.enabled:
            self._executor.submit(self._preload)

    def transcribe_async(self, audio: np.ndarray, request_id: int = 0) -> Future | None:
        if self._executor is None:
            self._bus.publish(Events.TRANSCRIPT_FAILED, {"error": "speech recognition not running", "request_id": request_id})
            return None
        return self._executor.submit(self._transcribe, audio, request_id)

    def _transcribe(self, audio: np.ndarray, request_id: int) -> TranscriptResult | None:
        if not self.enabled:
            self._bus.publish(Events.TRANSCRIPT_FAILED, {"error": "speech recognition is disabled", "request_id": request_id})
            return None
        try:
            model = self._pool.get(self.model_name, self.device, self.compute_type)
            result = transcribe_array(model, audio, self.language)
        except Exception as exc:
            log.exception("Transcription failed")
            self._status(False, f"error: {exc.__class__.__name__}")
            self._bus.publish(Events.TRANSCRIPT_FAILED, {"error": f"Speech recognition failed: {exc}", "request_id": request_id})
            return None
        if not self.ready:
            self._status(True, f"{self.model_name} ready ({self.device}/{self.compute_type})")
        if result.empty:
            self._bus.publish(Events.TRANSCRIPT_FAILED, {"error": "I couldn't make out any words.", "request_id": request_id, "empty": True})
        else:
            self._bus.publish(Events.TRANSCRIPT_READY, {
                "text": result.text, "language": result.language, "duration": result.duration,
                "confidence": result.confidence, "request_id": request_id,
            })
        return result

    def stop(self) -> None:
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
