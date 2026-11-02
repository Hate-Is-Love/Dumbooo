"""The single microphone stream shared by every audio consumer.

There is exactly one input stream for the whole application. The PortAudio
callback only copies samples into a bounded queue; all processing happens on
the audio pipeline thread. Calling ``start()`` twice never opens a second
stream.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable, Protocol

import numpy as np

from audio import FRAME_SAMPLES, SAMPLE_RATE
from audio import audio_devices

log = logging.getLogger(__name__)


class MicrophoneError(RuntimeError):
    pass


class StreamLike(Protocol):
    active: bool

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...


StreamFactory = Callable[..., StreamLike]


class LinearResampler:
    """Streaming resampler used only when the device cannot deliver 16 kHz.

    A short moving-average low-pass limits aliasing before linear
    interpolation. Quality is adequate for VAD and speech recognition.
    """

    def __init__(self, src_rate: int, dst_rate: int = SAMPLE_RATE) -> None:
        self.ratio = src_rate / dst_rate
        taps = max(1, int(round(self.ratio)))
        self._kernel = np.ones(taps, dtype=np.float32) / taps
        self._tail = np.zeros(taps - 1, dtype=np.float32)
        self._pos = 0.0
        self._prev = np.zeros(0, dtype=np.float32)

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.ratio == 1.0:
            return x
        filtered = np.convolve(np.concatenate([self._tail, x]), self._kernel, mode="valid").astype(np.float32)
        if len(self._kernel) > 1:
            self._tail = x[-(len(self._kernel) - 1):].copy()
        buf = np.concatenate([self._prev, filtered])
        positions = np.arange(self._pos, len(buf) - 1, self.ratio)
        out = np.interp(positions, np.arange(len(buf)), buf).astype(np.float32)
        consumed = int(positions[-1]) if len(positions) else 0
        next_pos = (positions[-1] + self.ratio) if len(positions) else self._pos
        self._prev = buf[consumed:]
        self._pos = next_pos - consumed
        return out


def _default_stream_factory(**kwargs) -> StreamLike:
    import sounddevice as sd

    return sd.InputStream(**kwargs)


class Microphone:
    name = "microphone"

    def __init__(self, device: str | None = None, stream_factory: StreamFactory | None = None,
                 max_queue_frames: int = 256) -> None:
        self.device_name = device
        self._factory = stream_factory or _default_stream_factory
        self._queue: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=max_queue_frames)
        self._lock = threading.Lock()
        self._stream: StreamLike | None = None
        self._pending = np.zeros(0, dtype=np.float32)
        self._resampler: LinearResampler | None = None
        self.last_error: str | None = None
        self.last_frame_time = 0.0
        self.streams_opened = 0
        self.dropped_frames = 0
        self.muted = False
        self._closing = False

    # -- stream management -------------------------------------------------
    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._stream is not None and bool(getattr(self._stream, "active", True))

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                return  # never open a duplicate stream
            self._open_locked()

    def _open_locked(self) -> None:
        try:
            index = audio_devices.resolve(self.device_name, "input")
        except LookupError as exc:
            self.last_error = str(exc)
            raise MicrophoneError(str(exc)) from exc
        attempts = [SAMPLE_RATE]
        try:
            info = next((d for d in audio_devices.input_devices() if d.index == index), None)
            native = int(info.default_samplerate) if info else None
            if native and native != SAMPLE_RATE:
                attempts.append(native)
        except Exception:
            pass
        last_exc: Exception | None = None
        for rate in attempts:
            try:
                stream = self._factory(
                    device=index, samplerate=rate, channels=1, dtype="float32",
                    blocksize=FRAME_SAMPLES if rate == SAMPLE_RATE else 0,
                    callback=self._callback, finished_callback=self._on_finished,
                )
                stream.start()
            except Exception as exc:
                last_exc = exc
                log.info("Microphone did not open at %d Hz: %s", rate, exc)
                continue
            self._resampler = LinearResampler(rate) if rate != SAMPLE_RATE else None
            self._stream = stream
            self._closing = False
            self._pending = np.zeros(0, dtype=np.float32)
            self.streams_opened += 1
            self.last_error = None
            self.last_frame_time = time.monotonic()
            log.info("Microphone open (device=%s, %d Hz)", self.device_name or "default", rate)
            return
        self.last_error = f"Could not open microphone: {last_exc}"
        raise MicrophoneError(self.last_error)

    def stop(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
            self._closing = True
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                log.debug("Microphone stop failed", exc_info=True)
            try:
                stream.close()
            except Exception:
                log.debug("Microphone close failed", exc_info=True)
            log.info("Microphone closed")
        self.clear()

    def restart(self, device: str | None = ...) -> None:  # type: ignore[assignment]
        if device is not ...:
            self.device_name = device
        self.stop()
        self.start()

    def clear(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    # -- data path -----------------------------------------------------------
    def _callback(self, indata, frames, time_info, status) -> None:  # PortAudio thread
        if status:
            log.debug("Microphone status: %s", status)
        samples = np.asarray(indata, dtype=np.float32).reshape(-1)
        self.feed(samples)

    def feed(self, samples: np.ndarray) -> None:
        """Accept raw samples at the stream rate (also used by tests)."""
        self.last_frame_time = time.monotonic()
        if self._resampler is not None:
            samples = self._resampler.process(samples)
        if self.muted:
            samples = np.zeros_like(samples)
        buf = np.concatenate([self._pending, samples]) if self._pending.size else samples
        n = len(buf) // FRAME_SAMPLES
        for i in range(n):
            frame = buf[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES].copy()
            try:
                self._queue.put_nowait(frame)
            except queue.Full:
                try:  # keep the newest audio; drop the oldest frame
                    self._queue.get_nowait()
                    self.dropped_frames += 1
                except queue.Empty:
                    pass
                self._queue.put_nowait(frame)
        self._pending = buf[n * FRAME_SAMPLES:].copy()

    def _on_finished(self) -> None:
        # Called by PortAudio when the stream stops for any reason, including
        # the device being unplugged.
        if not self._closing:
            log.warning("Microphone stream ended unexpectedly")

    def read(self, timeout: float = 0.1) -> np.ndarray | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
