"""Hardware and model stand-ins so tests run without microphones, speakers or downloads."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np

from audio import FRAME_SAMPLES, SAMPLE_RATE


def tone(seconds: float, freq: float = 220.0, amp: float = 0.2) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def frames(audio: np.ndarray) -> list[np.ndarray]:
    n = len(audio) // FRAME_SAMPLES
    return [audio[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES] for i in range(n)]


class FakeInputStream:
    """Stands in for sounddevice.InputStream. ``fail`` simulates a missing device."""

    instances: list["FakeInputStream"] = []
    fail = False

    def __init__(self, **kwargs) -> None:
        if FakeInputStream.fail:
            raise OSError("no input device")
        self.kwargs = kwargs
        self.callback = kwargs["callback"]
        self.active = False
        self.closed = False
        FakeInputStream.instances.append(self)

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False

    def close(self) -> None:
        self.closed = True

    def push(self, audio: np.ndarray) -> None:
        self.callback(audio.reshape(-1, 1), len(audio), None, None)

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.fail = False


class FakeOutputStream:
    """Stands in for sounddevice.OutputStream; pulls audio ~20x faster than real time."""

    instances: list["FakeOutputStream"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.callback = kwargs["callback"]
        self.rate = kwargs["samplerate"]
        self.active = False
        self.collected: list[np.ndarray] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.aborted = False
        FakeOutputStream.instances.append(self)

    def start(self) -> None:
        self.active = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        block = 1024
        while not self._stop.is_set():
            out = np.zeros((block, 1), dtype=np.float32)
            self.callback(out, block, None, None)
            self.collected.append(out[:, 0].copy())
            time.sleep(block / self.rate / 20)

    def stop(self) -> None:
        self._stop.set()
        self.active = False

    def abort(self) -> None:
        self.aborted = True
        self.stop()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(1)


class FakeSynth:
    """Piper stand-in: yields 0.25 s of tone per sentence."""

    sample_rate = 22050

    def __init__(self, *_args, seconds: float = 0.25) -> None:
        self.seconds = seconds
        self.spoken: list[str] = []

    def synthesize(self, text: str):
        self.spoken.append(text)
        t = np.arange(int(self.seconds * self.sample_rate)) / self.sample_rate
        yield (0.3 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)


class FakeWhisperModel:
    """faster-whisper stand-in returning fixed text (or raising)."""

    def __init__(self, text: str = "hello there", error: Exception | None = None,
                 no_speech_prob: float = 0.01, avg_logprob: float = -0.2, word_prob: float = 0.9,
                 hinted_text: str | None = None, hinted_word_prob: float | None = None) -> None:
        self.text, self.error = text, error
        self.no_speech_prob, self.avg_logprob = no_speech_prob, avg_logprob
        self.word_prob = word_prob
        self.hinted_text, self.hinted_word_prob = hinted_text, hinted_word_prob  # answer when given a prompt
        self.calls = 0
        self.prompts: list[str | None] = []
        self.kwargs: list[dict] = []

    def transcribe(self, audio, **kwargs):
        self.calls += 1
        self.prompts.append(kwargs.get("initial_prompt"))
        self.kwargs.append(kwargs)
        if self.error:
            raise self.error
        text, prob = self.text, self.word_prob
        if kwargs.get("initial_prompt") and self.hinted_text is not None:
            text, prob = self.hinted_text, self.hinted_word_prob if self.hinted_word_prob is not None else prob
        words = [SimpleNamespace(word=" " + w, probability=prob) for w in text.split()]
        segs = [SimpleNamespace(text=" " + text, no_speech_prob=self.no_speech_prob,
                                avg_logprob=self.avg_logprob, words=words)] if text else []
        return iter(segs), SimpleNamespace(language="en")


class AmplitudeVAD:
    """Deterministic VAD: probability from frame loudness."""

    engine = "fake-amplitude"

    def reset(self) -> None:
        pass

    def __call__(self, frame) -> float:
        rms = float(np.sqrt(np.mean(np.square(frame))))
        return 0.95 if rms > 0.02 else 0.02


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingBus:
    """Wraps an EventBus and records every event."""

    def __init__(self, bus) -> None:
        self.events: list[tuple[str, dict]] = []
        bus.subscribe("*", lambda n, p: self.events.append((n, p)))

    def names(self) -> list[str]:
        return [n for n, _ in self.events]

    def of(self, name: str) -> list[dict]:
        return [p for n, p in self.events if n == name]


def wait_for(predicate, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()
