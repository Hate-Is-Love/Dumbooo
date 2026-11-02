"""VAD, segmentation and noise suppression.

Segmenter tests feed exact per-frame probabilities, so they are deterministic.
The Silero and noise-suppression tests use real models/DSP on synthetic audio;
when the Piper voice is installed they also use real synthesized speech.
"""

from pathlib import Path

import numpy as np
import pytest

from audio import FRAME_MS, FRAME_SAMPLES, SAMPLE_RATE
from audio.noise_suppression import NoiseSuppressor
from audio.vad import EnergyVAD, SegmenterConfig, SileroVAD, SpeechSegmenter, silero_model_path
from tests.fakes import frames, tone

ROOT = Path(__file__).resolve().parent.parent
PIPER_VOICE = ROOT / "models" / "piper" / "en_US-lessac-medium.onnx"


def run(segmenter, probs, amp=0.1):
    events = []
    for i, p in enumerate(probs):
        frame = np.full(FRAME_SAMPLES, amp * (i + 1) / len(probs), dtype=np.float32)
        events += [(i, e) for e in segmenter.process(frame, p)]
    return events


def n(ms):
    return int(round(ms / FRAME_MS))


def cfg(**kw):
    base = dict(threshold=0.5, end_silence_ms=640, pre_roll_ms=320, min_speech_ms=256,
                max_record_s=5.0, listen_timeout_s=3.0)
    base.update(kw)
    return SegmenterConfig(**base)


def test_detects_start_and_end_with_pre_roll():
    seg = SpeechSegmenter(cfg())
    probs = [0.0] * n(640) + [0.9] * n(960) + [0.0] * n(800)
    events = run(seg, probs)
    kinds = [e.kind for _, e in events]
    assert kinds == ["start", "end"]
    start_frame = events[0][0]
    assert start_frame == n(640) + n(96) - 1  # onset confirmed after ~96 ms of speech
    end = events[1][1]
    # pre-roll (320 ms) + speech (960 ms) + kept trailing silence (~200 ms)
    expected = (n(320) + n(960) + n(200)) * FRAME_SAMPLES
    assert abs(len(end.audio) - expected) <= FRAME_SAMPLES
    assert not end.truncated


def test_natural_pause_does_not_end_utterance():
    seg = SpeechSegmenter(cfg(end_silence_ms=800))
    probs = [0.9] * n(500) + [0.1] * n(500) + [0.9] * n(500) + [0.0] * n(900)
    kinds = [e.kind for _, e in run(seg, probs)]
    assert kinds == ["start", "end"]


def test_end_after_configured_silence():
    seg = SpeechSegmenter(cfg(end_silence_ms=640))
    probs = [0.9] * n(500) + [0.0] * n(2000)
    events = run(seg, probs)
    end_frame = [i for i, e in events if e.kind == "end"][0]
    assert end_frame == n(500) + n(640) - 1


def test_short_blip_is_rejected():
    seg = SpeechSegmenter(cfg(min_speech_ms=300))
    probs = [0.9] * n(160) + [0.0] * n(1000)
    kinds = [e.kind for _, e in run(seg, probs)]
    assert kinds == ["start", "rejected"]


def test_single_frame_spike_never_starts():
    seg = SpeechSegmenter(cfg())
    probs = ([0.0] * 5 + [0.99]) * 20
    assert run(seg, probs[:n(2000)]) == []


def test_maximum_duration_truncates():
    seg = SpeechSegmenter(cfg(max_record_s=2.0))
    events = run(seg, [0.9] * n(4000))
    ends = [e for _, e in events if e.kind == "end"]
    assert ends and ends[0].truncated
    assert len(ends[0].audio) / SAMPLE_RATE <= 2.0 + FRAME_MS / 1000


def test_listen_timeout_when_nobody_speaks():
    seg = SpeechSegmenter(cfg(listen_timeout_s=1.0))
    events = run(seg, [0.0] * n(1100))
    assert [e.kind for _, e in events] == ["timeout"]


def test_hysteresis_keeps_speech_alive_between_thresholds():
    seg = SpeechSegmenter(cfg(end_silence_ms=320))
    probs = [0.9] * n(400) + [0.42] * n(1000) + [0.0] * n(400)  # 0.42: below 0.5 but above 0.35
    kinds = [(i, e.kind) for i, e in run(seg, probs)]
    end_frame = [i for i, k in kinds if k == "end"][0]
    assert end_frame >= n(1400)


# ---------------------------------------------------------------- Silero (real model)
needs_silero = pytest.mark.skipif(silero_model_path() is None, reason="faster-whisper (Silero model) not installed")


@needs_silero
def test_silero_quiet_on_silence_and_noise():
    vad = SileroVAD()
    rng = np.random.default_rng(0)
    silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
    noise = (rng.normal(0, 0.05, SAMPLE_RATE)).astype(np.float32)
    hum = tone(1.0, 50, 0.3)
    for audio in (silence, noise, hum):
        vad.reset()
        probs = [vad(f) for f in frames(audio)]
        assert max(probs) < 0.5, max(probs)


@needs_silero
def test_silero_rejects_wrong_frame_size():
    with pytest.raises(ValueError):
        SileroVAD()(np.zeros(100, dtype=np.float32))


def _speech() -> np.ndarray | None:
    if not PIPER_VOICE.exists():
        return None
    from audio.text_to_speech import PiperSynthesizer
    from audio.playback import resample

    synth = PiperSynthesizer(PIPER_VOICE)
    audio = np.concatenate(list(synth.synthesize("Hello DUMBOO, please add milk to my shopping list.")))
    return resample(audio, synth.sample_rate, SAMPLE_RATE)


needs_voice = pytest.mark.skipif(not PIPER_VOICE.exists(), reason="Piper voice not downloaded")


@needs_silero
@needs_voice
@pytest.mark.parametrize("gain", [0.15, 1.0, 3.0])  # quiet, normal, loud speakers
def test_silero_detects_real_speech_at_different_volumes(gain):
    speech = np.clip(_speech() * gain, -1, 1)
    vad = SileroVAD()
    probs = [vad(f) for f in frames(speech)]
    assert np.mean(np.array(probs) > 0.5) > 0.4


@needs_silero
@needs_voice
def test_segmenter_with_silero_on_speech_in_noise():
    rng = np.random.default_rng(1)
    speech = _speech()
    pad = np.zeros(SAMPLE_RATE, dtype=np.float32)
    audio = np.concatenate([pad, speech, pad, pad])
    audio = audio + rng.normal(0, 0.01, len(audio)).astype(np.float32)  # background noise, ~SNR 15-20 dB
    vad, ns = SileroVAD(), NoiseSuppressor(0.6)
    seg = SpeechSegmenter(cfg(end_silence_ms=700, max_record_s=15, listen_timeout_s=30))
    kinds = []
    for f in frames(audio):
        clean = ns.process(f)
        kinds += [e.kind for e in seg.process(clean, vad(clean))]
    assert kinds[:2] == ["start", "end"]


# ---------------------------------------------------------------- noise suppression
def _db(x):
    return 10 * np.log10(np.mean(np.square(x)) + 1e-12)


def test_noise_suppressor_reduces_stationary_noise():
    rng = np.random.default_rng(2)
    noise = rng.normal(0, 0.03, SAMPLE_RATE * 3).astype(np.float32)
    ns = NoiseSuppressor(strength=0.8)
    out = np.concatenate([ns.process(f) for f in frames(noise)])
    # after convergence (skip first second) the noise must drop by at least 10 dB
    assert _db(noise[SAMPLE_RATE:]) - _db(out[SAMPLE_RATE:]) > 10


def test_noise_suppressor_keeps_speech_like_signal():
    rng = np.random.default_rng(3)
    n_total = SAMPLE_RATE * 4
    noise = rng.normal(0, 0.01, n_total).astype(np.float32)
    signal = np.zeros(n_total, dtype=np.float32)
    burst = tone(0.6, 300, 0.2) + tone(0.6, 900, 0.1)
    start = int(2.5 * SAMPLE_RATE)
    signal[start:start + len(burst)] = burst
    ns = NoiseSuppressor(strength=0.8)
    out = np.concatenate([ns.process(f) for f in frames(noise + signal)])
    seg_in = (noise + signal)[start + 1600:start + len(burst) - 1600]
    seg_out = out[start + 1600 + 256:start + len(burst) - 1600 + 256]  # 16 ms processing delay
    assert _db(seg_in) - _db(seg_out) < 3  # the "voice" loses less than 3 dB


def test_noise_suppressor_disabled_is_passthrough():
    x = tone(0.1)[:FRAME_SAMPLES]
    ns = NoiseSuppressor(enabled=False)
    assert np.array_equal(ns.process(x), x)


def test_energy_fallback_vad_distinguishes_loudness():
    vad = EnergyVAD()
    for _ in range(30):
        vad(np.full(FRAME_SAMPLES, 0.001, dtype=np.float32))
    assert vad(np.full(FRAME_SAMPLES, 0.001, dtype=np.float32)) < 0.5
    assert vad(tone(0.1, 200, 0.3)[:FRAME_SAMPLES]) > 0.5
