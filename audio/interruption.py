"""Barge-in detection: decide when the user genuinely talks over DUMBOO.

The hard part is that DUMBOO's own voice, played through the speakers and
picked up by the microphone, *is speech*, so a VAD alone would interrupt
itself. This detector combines three checks per 32 ms frame:

1. Speech: Silero VAD probability above ``threshold`` (loud non-speech noise
   such as a door slam or keyboard scores low and is ignored).
2. Echo gate: the microphone level must exceed the level predicted from
   what the speaker is currently playing by ``echo_margin_db``. The
   speaker-to-microphone coupling is learned during the first half second of
   every utterance and tracked afterwards. This is playback-aware gating, not
   true acoustic echo cancellation.
3. Persistence: speech frames must add up to ``min_speech_ms`` inside a short
   sliding window, so a single cough does not stop playback.

Limits: with loud laptop speakers right next to the microphone the echo can
mask quiet speech, so the user may need to speak up or use headphones.
Headphones remove the problem entirely.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass

from audio import FRAME_MS

_EPS = 1e-6


@dataclass
class InterruptionConfig:
    threshold: float = 0.7
    min_speech_ms: int = 300
    echo_margin_db: float = 6.0
    grace_ms: int = 500          # learn the echo level; never interrupt this early
    reference_window_ms: int = 250  # speaker level is compared over this window (device latency)

    @classmethod
    def from_settings(cls, s: dict) -> "InterruptionConfig":
        return cls(threshold=float(s["threshold"]), min_speech_ms=int(s["min_speech_ms"]),
                   echo_margin_db=float(s["echo_margin_db"]))


class InterruptionDetector:
    def __init__(self, config: InterruptionConfig | None = None) -> None:
        self.config = config or InterruptionConfig()
        self.reset()

    def reset(self) -> None:
        c = self.config
        self._elapsed_ms = 0.0
        self._coupling: float | None = None
        window = max(1, int(round(c.min_speech_ms * 1.6 / FRAME_MS)))
        self._votes: collections.deque[bool] = collections.deque(maxlen=window)
        self._reference: collections.deque[float] = collections.deque(
            maxlen=max(1, int(round(c.reference_window_ms / FRAME_MS))))

    @property
    def coupling(self) -> float | None:
        return self._coupling

    def process(self, mic_rms: float, speech_prob: float, playback_rms: float) -> bool:
        """Feed one frame. Returns True once when an interruption is confirmed."""
        c = self.config
        self._elapsed_ms += FRAME_MS
        self._reference.append(playback_rms)
        ref = max(self._reference)
        margin = 10 ** (c.echo_margin_db / 20)

        if ref > 0.003:
            ratio = mic_rms / (ref + _EPS)
            if self._coupling is None:
                self._coupling = ratio
            elif self._elapsed_ms <= c.grace_ms:
                self._coupling = 0.85 * self._coupling + 0.15 * ratio
            elif ratio < self._coupling * margin:
                self._coupling = 0.98 * self._coupling + 0.02 * ratio  # slow tracking of echo only

        if self._elapsed_ms <= c.grace_ms:
            return False

        predicted_echo = (self._coupling or 0.0) * ref
        above_echo = ref <= 0.003 or mic_rms >= predicted_echo * margin
        vote = speech_prob >= c.threshold and above_echo
        self._votes.append(vote)
        needed = max(1, int(round(c.min_speech_ms / FRAME_MS)))
        if sum(self._votes) >= needed:
            self._votes.clear()
            return True
        return False
