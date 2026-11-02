"""The audio processing thread.

Pulls frames from the one shared microphone stream and runs, per 32 ms frame::

    raw frame -> noise suppression -> Silero VAD -> route by mode

Modes (set by the orchestrator from the assistant state):

``wake``      feed the wake-word service (SLEEPING / IDLE)
``listen``    feed the utterance segmenter (WAKING / LISTENING)
``speaking``  feed the interruption detector (SPEAKING)
``off``       analyse levels only (PROCESSING)

It also watches the microphone: if no frames arrive for two seconds it reports
the microphone as lost and retries opening it every few seconds.
Nothing here touches the UI; results are published on the event bus.
"""

from __future__ import annotations

import collections
import logging
import threading
import time

import numpy as np

from app.event_bus import EventBus, Events
from audio import FRAME_MS
from audio.interruption import InterruptionDetector
from audio.microphone import Microphone, MicrophoneError
from audio.noise_suppression import NoiseSuppressor
from audio.vad import SegmenterConfig, SpeechSegmenter, rms
from audio.wake_word import WakeWordService

log = logging.getLogger(__name__)

MODES = ("wake", "listen", "speaking", "off")


class AudioPipeline:
    name = "audio_pipeline"

    def __init__(self, bus: EventBus, mic: Microphone, vad, suppressor: NoiseSuppressor,
                 segmenter: SpeechSegmenter, interruption: InterruptionDetector, wake: WakeWordService,
                 playback_level=lambda: 0.0, stall_timeout_s: float = 2.0, retry_interval_s: float = 3.0,
                 clock=time.monotonic) -> None:
        self._bus = bus
        self.mic = mic
        self.vad = vad
        self.suppressor = suppressor
        self.segmenter = segmenter
        self.interruption = interruption
        self.wake = wake
        self._playback_level = playback_level
        self._stall_timeout = stall_timeout_s
        self._retry_interval = retry_interval_s
        self._clock = clock
        self._mode = "wake"
        self._applied_mode: str | None = None
        self._carry_over = False
        self._recent: collections.deque[tuple[np.ndarray, float]] = collections.deque(maxlen=int(1000 / FRAME_MS))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._frame_count = 0
        self.mic_ok = False
        self._next_retry = 0.0
        self.interruption_enabled = True

    # -- control (any thread) --------------------------------------------------
    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str, carry_over: bool = False) -> None:
        if mode not in MODES:
            raise ValueError(mode)
        with self._lock:
            self._mode = mode
            self._carry_over = carry_over

    def update_segmenter(self, config: SegmenterConfig) -> None:
        with self._lock:
            self.segmenter = SpeechSegmenter(config)
            self._applied_mode = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return  # never a second pipeline thread
        self._stop.clear()
        self._open_mic()
        self._thread = threading.Thread(target=self._run, name="AudioPipeline", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._thread = None
        self.mic.stop()
        self._set_mic_ok(False, None)

    # -- microphone health ----------------------------------------------------
    def _set_mic_ok(self, ok: bool, error: str | None) -> None:
        if ok != self.mic_ok or error:
            self.mic_ok = ok
            self._bus.publish(Events.MIC_STATUS, {"open": ok, "device": self.mic.device_name or "System default",
                                                  "error": error})

    def _open_mic(self) -> bool:
        try:
            self.mic.start()
        except MicrophoneError as exc:
            self._set_mic_ok(False, str(exc))
            self._next_retry = self._clock() + self._retry_interval
            return False
        self._set_mic_ok(True, None)
        return True

    def restart_mic(self, device: str | None) -> None:
        self.mic.stop()
        self.mic.device_name = device
        self._open_mic()

    def _check_mic(self) -> None:
        now = self._clock()
        if self.mic_ok and now - self.mic.last_frame_time > self._stall_timeout:
            log.warning("Microphone stopped delivering audio")
            self.mic.stop()
            self._set_mic_ok(False, "Microphone disconnected or stopped responding")
            self._next_retry = now + self._retry_interval
        elif not self.mic_ok and now >= self._next_retry:
            log.info("Retrying microphone")
            self._next_retry = now + self._retry_interval
            self._open_mic()

    # -- processing ------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            frame = self.mic.read(timeout=0.1)
            if frame is None:
                self._check_mic()
                continue
            try:
                self.process_frame(frame)
            except Exception:
                log.exception("Audio frame processing failed")
                self._bus.publish(Events.ERROR, {"source": "audio", "message": "Audio processing error (recovered)",
                                                 "recoverable": True})

    def _apply_mode(self) -> str:
        with self._lock:
            mode, carry = self._mode, self._carry_over
            self._carry_over = False
        if mode != self._applied_mode:
            if mode == "listen":
                self.segmenter.reset()
                if carry:  # interruption: the user is already talking; keep their first words
                    for frame, prob in list(self._recent):
                        for event in self.segmenter.process(frame, prob):
                            self._publish_segment(event)
            elif mode == "speaking":
                self.interruption.reset()
            self._applied_mode = mode
        return mode

    def process_frame(self, raw: np.ndarray) -> None:
        clean = self.suppressor.process(raw)
        prob = self.vad(clean)
        level = rms(clean)
        self._recent.append((clean, prob))
        self._frame_count += 1
        if self._frame_count % 3 == 0:
            self._bus.publish(Events.AUDIO_LEVEL, {"rms": level, "speech_prob": prob})

        mode = self._apply_mode()
        if mode == "wake":
            self.wake.process(raw, clean, prob)
        elif mode == "listen":
            for event in self.segmenter.process(clean, prob):
                self._publish_segment(event)
        elif mode == "speaking" and self.interruption_enabled:
            if self.interruption.process(level, prob, self._playback_level()):
                log.info("User interruption detected")
                self._bus.publish(Events.INTERRUPTION_DETECTED, {"speech_ms": self.interruption.config.min_speech_ms})

    def _publish_segment(self, event) -> None:
        if event.kind == "start":
            self._bus.publish(Events.SPEECH_STARTED, {})
        elif event.kind == "end":
            self._bus.publish(Events.SPEECH_ENDED, {"duration": event.duration})
            self._bus.publish(Events.UTTERANCE_CAPTURED, {"audio": event.audio, "duration": event.duration,
                                                          "truncated": event.truncated})
        elif event.kind == "rejected":
            self._bus.publish(Events.UTTERANCE_REJECTED, {"reason": event.reason})
        elif event.kind == "timeout":
            self._bus.publish(Events.LISTEN_TIMEOUT, {})
