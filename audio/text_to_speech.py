"""Local text-to-speech with Piper, played through ``AudioPlayer``.

A single worker thread synthesises sentence by sentence and streams audio to
the player, so speech starts after the first sentence is ready. Text can also
arrive piece by piece while an AI answer is still streaming
(``begin_stream`` / ``feed`` / ``end_stream``); ``speak()`` is the one-shot
form. A new utterance or ``stop_speaking()`` cancels the current one first:
playback never overlaps and no queued speech plays after a cancel.

Events: ``PLAYBACK_STARTED`` when audio begins, ``PLAYBACK_LEVEL`` (~20 Hz)
while it plays, and exactly one ``PLAYBACK_FINISHED`` per started utterance.
"""

from __future__ import annotations

import itertools
import logging
import queue
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol

import numpy as np

from app.event_bus import EventBus, Events
from audio.playback import AudioPlayer

log = logging.getLogger(__name__)


class Synthesizer(Protocol):
    sample_rate: int

    def synthesize(self, text: str) -> Iterable[np.ndarray]: ...


class PiperSynthesizer:
    def __init__(self, model_path: Path, speed: float = 1.0) -> None:
        if not model_path.exists():
            raise FileNotFoundError(f"Piper voice not found: {model_path}")
        config = Path(str(model_path) + ".json")
        if not config.exists():
            raise FileNotFoundError(f"Piper voice config not found: {config}")
        from piper import PiperVoice, SynthesisConfig

        self._voice = PiperVoice.load(str(model_path), config_path=str(config))
        self._config = SynthesisConfig(length_scale=1.0 / speed)
        self.sample_rate = int(self._voice.config.sample_rate)
        self.name = model_path.stem

    def synthesize(self, text: str) -> Iterable[np.ndarray]:
        for chunk in self._voice.synthesize(text, syn_config=self._config):
            yield np.asarray(chunk.audio_float_array, dtype=np.float32)


_SENTENCE = re.compile(r"(?<=[.!?;:])\s+")


def split_sentences(text: str, max_len: int = 240) -> list[str]:
    parts: list[str] = []
    for sentence in _SENTENCE.split(text.strip()):
        sentence = sentence.strip()
        while len(sentence) > max_len:  # break very long sentences at a space
            cut = sentence.rfind(" ", 0, max_len)
            cut = cut if cut > 0 else max_len
            parts.append(sentence[:cut])
            sentence = sentence[cut:].strip()
        if sentence:
            parts.append(sentence)
    return parts


@dataclass
class _Request:
    utterance_id: int
    pieces: "queue.Queue[str | None]" = field(default_factory=queue.Queue)  # None = end of text
    cancel: threading.Event = field(default_factory=threading.Event)


class TextToSpeech:
    name = "tts"

    def __init__(self, bus: EventBus, player: AudioPlayer, model_path: Path, speed: float = 1.0,
                 enabled: bool = True, synth_factory: Callable[[Path, float], Synthesizer] | None = None) -> None:
        self._bus = bus
        self._player = player
        self._model_path = model_path
        self._speed = speed
        self.enabled = enabled
        self._synth_factory = synth_factory or PiperSynthesizer
        self._synth: Synthesizer | None = None
        self._queue: "queue.Queue[_Request | None]" = queue.Queue()
        self._ids = itertools.count(1)
        self._current: _Request | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.ready = False
        self.detail = "not started"

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._status(False, "loading voice…")
            # The voice loads on the worker thread (several seconds) so start-up never blocks the UI.
            self._thread = threading.Thread(target=self._run, name="TTS", daemon=True)
            self._thread.start()

    def _load_voice(self) -> None:
        if not self.enabled:
            self._status(False, "voice output disabled in settings")
            return
        try:
            self._synth = self._synth_factory(self._model_path, self._speed)
            self._status(True, f"Piper voice {self._model_path.stem}")
        except FileNotFoundError as exc:
            self._synth = None
            self._status(False, f"{exc}. Run: python scripts/download_models.py")
        except Exception as exc:
            self._synth = None
            log.exception("Piper voice failed to load")
            self._status(False, f"voice failed to load: {exc}")

    def reconfigure(self, model_path: Path, speed: float, enabled: bool) -> None:
        self.stop_speaking()
        same_voice = self._synth is not None and model_path == self._model_path and speed == self._speed
        self._model_path, self._speed, self.enabled = model_path, speed, enabled
        if same_voice:  # only switching voice output on/off: no need to reload the voice
            self._status(enabled, f"Piper voice {model_path.stem}" if enabled else "voice output off")
            return
        self._load_voice()

    def _status(self, ready: bool, detail: str) -> None:
        self.ready, self.detail = ready, detail
        self._bus.publish(Events.TTS_STATUS, {"ready": ready, "detail": detail})

    def stop(self) -> None:
        self.stop_speaking()
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is not None:
            self._queue.put(None)
            thread.join(timeout=3)
        self._player.abort()

    # -- public API ------------------------------------------------------------
    @property
    def speaking(self) -> bool:
        return self._player.active

    @property
    def available(self) -> bool:
        return self.enabled and self.ready and self._thread is not None

    def begin_stream(self) -> int | None:
        """Start a new utterance whose text arrives later via ``feed``. None if TTS is unavailable."""
        if not self.available:
            return None
        self.stop_speaking()
        request = _Request(next(self._ids))
        with self._lock:
            self._current = request
        self._queue.put(request)
        return request.utterance_id

    def feed(self, utterance_id: int, text: str) -> None:
        request = self._current
        if request is not None and request.utterance_id == utterance_id and text.strip():
            request.pieces.put(text)

    def end_stream(self, utterance_id: int) -> None:
        request = self._current
        if request is not None and request.utterance_id == utterance_id:
            request.pieces.put(None)

    def speak(self, text: str) -> int | None:
        """Speak ``text`` in one go; returns the utterance id, or None if TTS is unavailable."""
        if not text.strip():
            return None
        utterance_id = self.begin_stream()
        if utterance_id is not None:
            self.feed(utterance_id, text)
            self.end_stream(utterance_id)
        return utterance_id

    def stop_speaking(self) -> None:
        with self._lock:
            request, self._current = self._current, None
        if request is not None:
            request.cancel.set()
        self._player.abort()
        # Drop utterances that have not started yet.
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                self._queue.put(None)
                break

    # -- worker ----------------------------------------------------------------
    def _run(self) -> None:
        self._load_voice()
        while True:
            request = self._queue.get()
            if request is None:
                return
            if request.cancel.is_set():
                continue
            self._play(request)

    def _play(self, request: _Request) -> None:
        synth = self._synth
        started = False
        interrupted = False
        metering = threading.Event()  # set = stop the level meter
        try:
            if synth is None:
                raise RuntimeError("no voice loaded")
            while True:
                if request.cancel.is_set():
                    raise _Cancelled
                try:
                    piece = request.pieces.get(timeout=0.05)
                except queue.Empty:
                    continue
                if piece is None:
                    break
                for sentence in split_sentences(piece):
                    for audio in synth.synthesize(sentence):
                        if request.cancel.is_set():
                            raise _Cancelled
                        if not started:
                            self._player.begin(synth.sample_rate)
                            started = True
                            self._bus.publish(Events.PLAYBACK_STARTED, {"utterance_id": request.utterance_id})
                            threading.Thread(target=self._meter, args=(metering,), name="tts-level",
                                             daemon=True).start()
                        self._player.append(audio)
            if not started:
                return
            self._player.end_of_stream()
            while not self._player.wait(0.05):
                pass
            interrupted = request.cancel.is_set()
            if not interrupted:
                self._player.finish()
        except _Cancelled:
            interrupted = True
        except Exception as exc:
            log.exception("Speech playback failed")
            interrupted = True
            self._player.abort()
            self._bus.publish(Events.ERROR, {"source": "tts", "message": f"Speech playback failed: {exc}", "recoverable": True})
        finally:
            metering.set()
            # Exactly one FINISHED per utterance that reached the worker, even if
            # synthesis failed (or no text arrived) before any audio played.
            self._bus.publish(Events.PLAYBACK_FINISHED, {
                "utterance_id": request.utterance_id, "interrupted": interrupted, "started": started,
            })


    def _meter(self, stop: threading.Event) -> None:
        """~20 Hz PLAYBACK_LEVEL for as long as speech plays, also while the next sentence is synthesised."""
        while not stop.wait(0.05):
            self._bus.publish(Events.PLAYBACK_LEVEL, {"rms": self._player.level})


class _Cancelled(Exception):
    pass
