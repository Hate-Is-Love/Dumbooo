"""Microphone management, the audio pipeline and interruption detection (no hardware)."""

import numpy as np
import pytest

from app.event_bus import EventBus, Events
from audio import FRAME_MS, FRAME_SAMPLES, SAMPLE_RATE
from audio.interruption import InterruptionConfig, InterruptionDetector
from audio.microphone import LinearResampler, Microphone, MicrophoneError
from audio.noise_suppression import NoiseSuppressor
from audio.pipeline import AudioPipeline
from audio.vad import SegmenterConfig, SpeechSegmenter
from tests.fakes import AmplitudeVAD, FakeClock, FakeInputStream, RecordingBus, frames, tone


@pytest.fixture(autouse=True)
def _reset_streams():
    FakeInputStream.reset()
    yield
    FakeInputStream.reset()


# ---------------------------------------------------------------- microphone
def test_microphone_never_opens_duplicate_streams():
    mic = Microphone(stream_factory=FakeInputStream)
    mic.start()
    mic.start()
    mic.start()
    assert mic.streams_opened == 1 and len(FakeInputStream.instances) == 1
    mic.stop()
    assert FakeInputStream.instances[0].closed
    mic.start()
    assert mic.streams_opened == 2


def test_microphone_rechunks_into_vad_frames():
    mic = Microphone(stream_factory=FakeInputStream)
    mic.start()
    stream = FakeInputStream.instances[0]
    stream.push(np.ones(700, dtype=np.float32))
    stream.push(np.ones(400, dtype=np.float32))
    assert mic.read(0.01).shape == (FRAME_SAMPLES,)
    assert mic.read(0.01).shape == (FRAME_SAMPLES,)
    assert mic.read(0.01) is None  # 76 samples still pending


def test_microphone_queue_drops_oldest_when_full():
    mic = Microphone(stream_factory=FakeInputStream, max_queue_frames=4)
    mic.start()
    for i in range(6):
        FakeInputStream.instances[0].push(np.full(FRAME_SAMPLES, i, dtype=np.float32))
    got = [mic.read(0.01)[0] for _ in range(4)]
    assert got == [2, 3, 4, 5] and mic.dropped_frames == 2


def test_muted_microphone_delivers_silence():
    mic = Microphone(stream_factory=FakeInputStream)
    mic.start()
    mic.muted = True
    FakeInputStream.instances[0].push(np.ones(FRAME_SAMPLES, dtype=np.float32))
    assert not mic.read(0.01).any()


def test_missing_microphone_raises_clear_error():
    FakeInputStream.fail = True
    mic = Microphone(stream_factory=FakeInputStream)
    with pytest.raises(MicrophoneError):
        mic.start()
    assert "Could not open microphone" in mic.last_error


def test_resampler_output_length():
    r = LinearResampler(48000)
    out = np.concatenate([r.process(np.zeros(4800, dtype=np.float32)) for _ in range(10)])
    assert abs(len(out) - 16000) <= 2


# ---------------------------------------------------------------- pipeline
class FakeWake:
    def __init__(self):
        self.frames = 0

    def process(self, raw, clean, prob):
        self.frames += 1


def make_pipeline(clock=None):
    bus = EventBus(synchronous=True)
    rec = RecordingBus(bus)
    mic = Microphone(stream_factory=FakeInputStream)
    wake = FakeWake()
    seg = SpeechSegmenter(SegmenterConfig(end_silence_ms=320, min_speech_ms=200, listen_timeout_s=5, max_record_s=10))
    pipe = AudioPipeline(bus, mic, AmplitudeVAD(), NoiseSuppressor(enabled=False), seg, InterruptionDetector(),
                         wake, playback_level=lambda: 0.0, clock=clock or FakeClock())
    return pipe, rec, wake


def feed(pipe, audio):
    for f in frames(audio):
        pipe.process_frame(f)


def test_wake_mode_feeds_wake_service_only():
    pipe, rec, wake = make_pipeline()
    feed(pipe, tone(1.0))
    assert wake.frames == len(frames(tone(1.0)))
    assert Events.SPEECH_STARTED not in rec.names()


def test_listen_mode_produces_utterance():
    pipe, rec, wake = make_pipeline()
    pipe.set_mode("listen")
    feed(pipe, np.concatenate([np.zeros(8000, np.float32), tone(1.0), np.zeros(16000, np.float32)]))
    assert wake.frames == 0
    assert rec.names().count(Events.SPEECH_STARTED) == 1
    captured = rec.of(Events.UTTERANCE_CAPTURED)
    assert len(captured) == 1 and 1.0 <= captured[0]["duration"] <= 1.8


def test_off_mode_only_reports_levels():
    pipe, rec, wake = make_pipeline()
    pipe.set_mode("off")
    feed(pipe, tone(1.0))
    assert set(rec.names()) == {Events.AUDIO_LEVEL}


def test_interruption_carry_over_keeps_first_words():
    pipe, rec, _ = make_pipeline()
    pipe.set_mode("speaking")
    feed(pipe, tone(0.5))  # user starts talking during playback (no echo: playback level 0)
    pipe.set_mode("listen", carry_over=True)
    feed(pipe, np.concatenate([tone(0.3), np.zeros(16000, np.float32)]))
    captured = rec.of(Events.UTTERANCE_CAPTURED)
    assert captured and captured[0]["duration"] >= 0.7  # includes audio from before the switch


def test_microphone_disconnect_is_detected_and_retried():
    clock = FakeClock()
    pipe, rec, _ = make_pipeline(clock)
    pipe._open_mic()
    assert pipe.mic_ok
    pipe.mic.last_frame_time = clock.now
    clock.advance(3.0)  # no frames for 3 s
    FakeInputStream.fail = True
    pipe._check_mic()
    assert not pipe.mic_ok
    lost = [p for p in rec.of(Events.MIC_STATUS) if not p["open"]]
    assert lost and "disconnected" in lost[-1]["error"]
    clock.advance(1.0)
    pipe._check_mic()  # too early to retry
    assert not pipe.mic_ok
    clock.advance(3.0)
    FakeInputStream.fail = False
    pipe._check_mic()
    assert pipe.mic_ok and rec.of(Events.MIC_STATUS)[-1]["open"]


def test_pipeline_thread_start_stop_is_idempotent():
    pipe, _, _ = make_pipeline()
    pipe.start()
    pipe.start()
    assert pipe.running and pipe.mic.streams_opened == 1
    pipe.stop()
    assert not pipe.running and not pipe.mic.is_open


# ---------------------------------------------------------------- interruption detector
def frames_for(ms):
    return int(ms / FRAME_MS)


def drive(det, n, mic, prob, play):
    return any(det.process(mic, prob, play) for _ in range(n))


def test_own_voice_echo_does_not_interrupt():
    det = InterruptionDetector(InterruptionConfig(threshold=0.7, min_speech_ms=300, echo_margin_db=6))
    # speaker plays at 0.2 RMS, mic hears echo at ~0.05 (coupling 0.25); VAD says "speech" (it is our voice)
    assert not drive(det, frames_for(3000), mic=0.05, prob=0.95, play=0.2)


def test_user_speaking_over_echo_interrupts():
    det = InterruptionDetector(InterruptionConfig(threshold=0.7, min_speech_ms=300, echo_margin_db=6))
    drive(det, frames_for(800), mic=0.05, prob=0.95, play=0.2)  # learn echo
    assert drive(det, frames_for(400), mic=0.2, prob=0.95, play=0.2)  # user ~12 dB above echo


def test_loud_non_speech_noise_does_not_interrupt():
    det = InterruptionDetector()
    drive(det, frames_for(800), mic=0.05, prob=0.9, play=0.2)
    assert not drive(det, frames_for(2000), mic=0.6, prob=0.1, play=0.2)  # door slam, music hit


def test_brief_cough_does_not_interrupt():
    det = InterruptionDetector(InterruptionConfig(min_speech_ms=300))
    drive(det, frames_for(800), mic=0.05, prob=0.9, play=0.2)
    assert not drive(det, frames_for(128), mic=0.3, prob=0.95, play=0.2)
    assert not drive(det, frames_for(1000), mic=0.05, prob=0.2, play=0.2)


def test_no_interruption_during_grace_period():
    det = InterruptionDetector(InterruptionConfig(grace_ms=500))
    assert not drive(det, frames_for(480), mic=0.5, prob=0.99, play=0.0)


def test_speech_during_playback_pause_interrupts():
    det = InterruptionDetector()
    drive(det, frames_for(800), mic=0.05, prob=0.9, play=0.2)
    # gap between sentences: nothing playing, user talks
    det._reference.clear()
    assert drive(det, frames_for(600), mic=0.08, prob=0.95, play=0.0)
