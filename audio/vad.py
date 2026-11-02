"""Voice activity detection and utterance segmentation.

``SileroVAD`` runs the Silero VAD v6 neural network (ONNX, bundled with the
faster-whisper package) one 32 ms frame at a time, carrying its recurrent
state between frames. If the model cannot be loaded, ``EnergyVAD`` is used
instead and the fallback is reported in diagnostics; it is far less robust to
noise, so it is never used silently.

``SpeechSegmenter`` turns per-frame speech probabilities into complete
utterances: onset confirmation, pre-roll, natural-pause tolerance, end of
speech after a configurable silence, a maximum duration and rejection of
too-short sounds.
"""

from __future__ import annotations

import collections
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from audio import FRAME_MS, FRAME_SAMPLES, SAMPLE_RATE

log = logging.getLogger(__name__)

CONTEXT_SAMPLES = 64


def silero_model_path() -> Path | None:
    try:
        import faster_whisper
    except ImportError:
        return None
    path = Path(faster_whisper.__file__).parent / "assets" / "silero_vad_v6.onnx"
    return path if path.exists() else None


class SileroVAD:
    engine = "silero-v6"

    def __init__(self, model_path: Path | None = None) -> None:
        import onnxruntime as ort

        path = model_path or silero_model_path()
        if path is None:
            raise FileNotFoundError("Silero VAD model not found (is faster-whisper installed?)")
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self._session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"], sess_options=opts)
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        frame = np.asarray(frame, dtype=np.float32)
        if frame.shape != (FRAME_SAMPLES,):
            raise ValueError(f"VAD expects {FRAME_SAMPLES} samples, got {frame.shape}")
        x = np.concatenate([self._context, frame])[None, :]
        prob, self._h, self._c = self._session.run(None, {"input": x, "h": self._h, "c": self._c})
        self._context = frame[-CONTEXT_SAMPLES:]
        return float(np.asarray(prob).reshape(-1)[0])


class EnergyVAD:
    """Adaptive-threshold energy detector. Fallback only."""

    engine = "energy-fallback"

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._floor = 1e-4

    def __call__(self, frame: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(np.square(frame))) + 1e-9)
        if rms < self._floor:
            self._floor = 0.7 * self._floor + 0.3 * rms  # follow quiet periods down quickly
        else:
            self._floor = min(rms, self._floor * 1.05)  # creep up slowly so speech isn't learned as noise
        snr_db = 20 * np.log10(rms / (self._floor + 1e-9))
        return float(1 / (1 + np.exp(-(snr_db - 12) / 3)))


def create_vad() -> SileroVAD | EnergyVAD:
    try:
        return SileroVAD()
    except Exception as exc:
        log.error("Silero VAD unavailable, using energy fallback: %s", exc)
        return EnergyVAD()


def rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(frame, dtype=np.float32))))


@dataclass
class SegmenterConfig:
    threshold: float = 0.5
    end_silence_ms: int = 900
    pre_roll_ms: int = 400
    min_speech_ms: int = 300
    max_record_s: float = 15.0
    listen_timeout_s: float = 8.0
    onset_ms: int = 96            # speech must persist this long to count as onset
    trailing_keep_ms: int = 200   # silence kept after the last speech frame

    @classmethod
    def from_settings(cls, vad: dict) -> "SegmenterConfig":
        return cls(
            threshold=float(vad["threshold"]),
            end_silence_ms=int(vad["end_silence_ms"]),
            pre_roll_ms=int(vad["pre_roll_ms"]),
            min_speech_ms=int(vad["min_speech_ms"]),
            max_record_s=float(vad["max_record_s"]),
            listen_timeout_s=float(vad["listen_timeout_s"]),
        )


@dataclass
class SegmentEvent:
    kind: str                      # "start" | "end" | "rejected" | "timeout"
    audio: np.ndarray | None = None
    duration: float = 0.0
    reason: str = ""
    truncated: bool = False


def _frames(ms: float) -> int:
    return max(1, int(round(ms / FRAME_MS)))


@dataclass
class SpeechSegmenter:
    config: SegmenterConfig = field(default_factory=SegmenterConfig)

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        c = self.config
        self._pre_roll: collections.deque[np.ndarray] = collections.deque(maxlen=max(1, _frames(c.pre_roll_ms)))
        self._onset: list[np.ndarray] = []
        self._buffer: list[np.ndarray] = []
        self._in_speech = False
        self._silence_frames = 0
        self._voiced_frames = 0
        self._last_voiced_index = 0
        self._waiting_frames = 0

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def process(self, frame: np.ndarray, speech_prob: float) -> list[SegmentEvent]:
        c = self.config
        events: list[SegmentEvent] = []
        voiced = speech_prob >= c.threshold
        quiet = speech_prob < max(0.05, c.threshold - 0.15)  # hysteresis

        if not self._in_speech:
            self._waiting_frames += 1
            if voiced:
                self._onset.append(frame)
                if len(self._onset) >= _frames(c.onset_ms):
                    self._in_speech = True
                    self._buffer = list(self._pre_roll) + self._onset
                    self._voiced_frames = len(self._onset)
                    self._last_voiced_index = len(self._buffer)
                    self._silence_frames = 0
                    self._onset = []
                    self._pre_roll.clear()
                    events.append(SegmentEvent("start"))
            else:
                for f in self._onset:  # a blip that did not persist becomes pre-roll
                    self._pre_roll.append(f)
                self._onset = []
                self._pre_roll.append(frame)
                if self._waiting_frames * FRAME_MS >= c.listen_timeout_s * 1000:
                    self._waiting_frames = 0
                    events.append(SegmentEvent("timeout", reason="no speech detected"))
            return events

        self._buffer.append(frame)
        if voiced:
            self._voiced_frames += 1
            self._silence_frames = 0
            self._last_voiced_index = len(self._buffer)
        elif quiet:
            self._silence_frames += 1
        # Frames between the two thresholds neither extend speech nor count as silence.

        duration_ms = len(self._buffer) * FRAME_MS
        if self._silence_frames >= _frames(c.end_silence_ms):
            events.append(self._finish(truncated=False))
        elif duration_ms >= c.max_record_s * 1000:
            events.append(self._finish(truncated=True))
        return events

    def _finish(self, truncated: bool) -> SegmentEvent:
        c = self.config
        keep = len(self._buffer) if truncated else min(len(self._buffer), self._last_voiced_index + _frames(c.trailing_keep_ms))
        audio = np.concatenate(self._buffer[:keep]) if keep else np.zeros(0, dtype=np.float32)
        voiced_ms = self._voiced_frames * FRAME_MS
        self.reset()
        if voiced_ms < c.min_speech_ms:
            return SegmentEvent("rejected", reason=f"too short ({voiced_ms:.0f} ms of speech)")
        return SegmentEvent("end", audio=audio, duration=len(audio) / SAMPLE_RATE, truncated=truncated)
