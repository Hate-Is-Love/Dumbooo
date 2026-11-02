"""Streaming spectral noise suppression for the microphone input.

Algorithm (classic single-channel speech enhancement, numpy only):

1. Short-time Fourier transform: 512-point FFT, 256-sample hop, sqrt-Hann
   analysis and synthesis windows (perfect reconstruction at 50% overlap).
2. Noise power spectrum tracked per frequency bin with a minimum-statistics
   style tracker: it follows quiet periods down quickly and rises slowly, so
   short speech bursts do not get learned as "noise".
3. Wiener gain with decision-directed a-priori SNR estimation, which keeps
   "musical noise" artefacts low.
4. A gain floor (set by ``strength``) avoids over-suppressing speech.
5. Bins below 80 Hz (rumble, desk knocks, DC offset) are removed.

This reduces steady background noise such as fans, air-conditioning and hum.
It cannot remove other people talking, music with vocals, or very sudden loud
sounds; those are handled by the VAD and interruption thresholds instead.
Latency is 16 ms (one hop).
"""

from __future__ import annotations

import numpy as np

from audio import FRAME_SAMPLES, SAMPLE_RATE

N_FFT = 512
HOP = 256


class NoiseSuppressor:
    def __init__(self, strength: float = 0.6, enabled: bool = True, sample_rate: int = SAMPLE_RATE) -> None:
        self.enabled = enabled
        self.strength = strength
        self._window = np.sqrt(np.hanning(N_FFT + 1)[:N_FFT]).astype(np.float32)
        freqs = np.fft.rfftfreq(N_FFT, 1 / sample_rate)
        self._highpass = np.clip((freqs - 60.0) / 40.0, 0.0, 1.0).astype(np.float32)  # 0 below 60 Hz, 1 above 100 Hz
        self.reset()

    @property
    def strength(self) -> float:
        return self._strength

    @strength.setter
    def strength(self, value: float) -> None:
        self._strength = float(np.clip(value, 0.0, 1.0))
        # strength 0 -> floor 1.0 (no suppression), 1 -> floor 0.05 (-26 dB)
        self._gain_floor = 1.0 - 0.95 * self._strength

    def reset(self) -> None:
        self._in_buf = np.zeros(N_FFT - HOP, dtype=np.float32)
        self._out_buf = np.zeros(N_FFT, dtype=np.float32)
        self._noise: np.ndarray | None = None
        self._smoothed: np.ndarray | None = None
        self._prev_gain = np.ones(N_FFT // 2 + 1, dtype=np.float32)
        self._prev_post = np.ones(N_FFT // 2 + 1, dtype=np.float32)
        self._frames_seen = 0

    @property
    def noise_level_db(self) -> float | None:
        if self._noise is None:
            return None
        return float(10 * np.log10(np.mean(self._noise) + 1e-12))

    def process(self, frame: np.ndarray) -> np.ndarray:
        """Process one 512-sample frame; returns 512 samples (16 ms delayed)."""
        frame = np.asarray(frame, dtype=np.float32)
        if not self.enabled:
            return frame
        out = np.empty(FRAME_SAMPLES, dtype=np.float32)
        for k in range(FRAME_SAMPLES // HOP):
            hop = frame[k * HOP:(k + 1) * HOP]
            block = np.concatenate([self._in_buf, hop])
            self._in_buf = block[HOP:]
            out[k * HOP:(k + 1) * HOP] = self._process_block(block)
        return out

    def _process_block(self, block: np.ndarray) -> np.ndarray:
        spec = np.fft.rfft(block * self._window)
        power = (spec.real ** 2 + spec.imag ** 2).astype(np.float32)
        self._update_noise(power)
        post = power / (self._noise + 1e-10)
        prior = 0.98 * (self._prev_gain ** 2) * self._prev_post + 0.02 * np.maximum(post - 1.0, 0.0)
        gain = prior / (1.0 + prior)
        gain = np.maximum(gain, self._gain_floor) * self._highpass
        self._prev_gain, self._prev_post = gain, post
        frame = np.fft.irfft(spec * gain, N_FFT).astype(np.float32) * self._window
        self._out_buf += frame
        result = self._out_buf[:HOP].copy()
        self._out_buf = np.concatenate([self._out_buf[HOP:], np.zeros(HOP, dtype=np.float32)])
        return result

    def _update_noise(self, power: np.ndarray) -> None:
        self._frames_seen += 1
        if self._noise is None:
            self._noise = power.copy() + 1e-10
            self._smoothed = power.copy()
            return
        self._smoothed = 0.7 * self._smoothed + 0.3 * power
        if self._frames_seen < 20:  # first ~160 ms: plain average to bootstrap
            self._noise = 0.8 * self._noise + 0.2 * power + 1e-10
            return
        falling = self._smoothed < self._noise
        self._noise = np.where(
            falling,
            0.6 * self._noise + 0.4 * self._smoothed,
            self._noise * 1.004,  # ~ +1 dB per second upward drift
        ).astype(np.float32) + 1e-10
