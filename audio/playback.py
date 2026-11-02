"""Speaker output with streaming, immediate stop and a level meter.

Only one utterance plays at a time: ``begin()`` aborts whatever is playing.
The PortAudio callback just copies queued samples to the device; it outputs
silence on an underrun and flags completion once the stream is finished.
Opening, draining and closing the stream happen on the caller's thread.
"""

from __future__ import annotations

import collections
import logging
import threading
from typing import Callable

import numpy as np

from audio import audio_devices

log = logging.getLogger(__name__)


def _default_stream_factory(**kwargs):
    import sounddevice as sd

    return sd.OutputStream(**kwargs)


def resample(audio: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or audio.size == 0:
        return audio
    n = int(round(len(audio) * dst / src))
    return np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)


class AudioPlayer:
    name = "playback"

    def __init__(self, device: str | None = None, stream_factory: Callable[..., object] | None = None) -> None:
        self.device_name = device
        self._factory = stream_factory or _default_stream_factory
        self._lock = threading.RLock()
        self._stream = None
        self._chunks: collections.deque[np.ndarray] = collections.deque()
        self._current = np.zeros(0, dtype=np.float32)
        self._ended = False
        self._done = threading.Event()
        self._done.set()
        self._source_rate = 22050
        self._stream_rate = 22050
        self.level = 0.0
        self.samples_played = 0
        self.last_error: str | None = None

    @property
    def active(self) -> bool:
        return not self._done.is_set()

    def begin(self, sample_rate: int) -> None:
        """Abort any current playback and open a stream for a new utterance."""
        self.abort()
        with self._lock:
            self._chunks.clear()
            self._current = np.zeros(0, dtype=np.float32)
            self._ended = False
            self._done.clear()
            self.level = 0.0
            self.samples_played = 0
            self._source_rate = sample_rate
            index = audio_devices.resolve(self.device_name, "output")
            last_exc = None
            for rate in (sample_rate, 48000, 44100):
                try:
                    stream = self._factory(device=index, samplerate=rate, channels=1, dtype="float32",
                                           callback=self._callback)
                    stream.start()
                except Exception as exc:
                    last_exc = exc
                    continue
                self._stream, self._stream_rate = stream, rate
                self.last_error = None
                return
            self._done.set()
            self.last_error = f"Could not open speaker: {last_exc}"
            raise RuntimeError(self.last_error)

    def append(self, audio: np.ndarray) -> None:
        audio = resample(np.asarray(audio, dtype=np.float32).reshape(-1), self._source_rate, self._stream_rate)
        with self._lock:
            self._chunks.append(audio)

    def end_of_stream(self) -> None:
        with self._lock:
            self._ended = True

    def _callback(self, outdata, frames, time_info, status) -> None:  # PortAudio thread
        out = np.zeros(frames, dtype=np.float32)
        filled = 0
        with self._lock:
            while filled < frames:
                if self._current.size == 0:
                    if not self._chunks:
                        break
                    self._current = self._chunks.popleft()
                take = min(frames - filled, self._current.size)
                out[filled:filled + take] = self._current[:take]
                self._current = self._current[take:]
                filled += take
            finished = self._ended and not self._chunks and self._current.size == 0
        self.samples_played += filled
        self.level = float(np.sqrt(np.mean(out[:filled] ** 2))) if filled else 0.0
        outdata[:, 0] = out
        if finished:
            self._done.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def finish(self) -> None:
        """Called after ``wait()`` returns: let the device drain, then close."""
        self._close(abort=False)

    def abort(self) -> None:
        """Stop immediately (used for interruptions and the Stop button)."""
        with self._lock:
            self._chunks.clear()
            self._current = np.zeros(0, dtype=np.float32)
            self._ended = True
        self._done.set()
        self._close(abort=True)

    def _close(self, abort: bool) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            (stream.abort if abort else stream.stop)()
        except Exception:
            log.debug("Speaker stop failed", exc_info=True)
        try:
            stream.close()
        except Exception:
            log.debug("Speaker close failed", exc_info=True)
        self.level = 0.0

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.abort()
