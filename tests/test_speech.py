"""Speech-to-text and text-to-speech services with fake models and a fake speaker."""

from pathlib import Path

import numpy as np
import pytest

from app.event_bus import EventBus, Events
from audio import SAMPLE_RATE
from audio.playback import AudioPlayer
from audio.speech_to_text import SpeechToText, WhisperModelPool, transcribe_array
from audio.text_to_speech import TextToSpeech, split_sentences
from tests.fakes import FakeOutputStream, FakeSynth, FakeWhisperModel, RecordingBus, wait_for

STT_SETTINGS = {"enabled": True, "model": "base.en", "language": "en", "device": "cpu", "compute_type": "int8"}


# ---------------------------------------------------------------- STT
def test_model_pool_loads_each_model_once(tmp_path):
    pool = WhisperModelPool(tmp_path, lambda name, **kw: FakeWhisperModel())
    assert pool.get("base.en") is pool.get("base.en")
    pool.get("tiny.en")
    assert pool.load_count == 2


def make_stt(tmp_path, model):
    bus = EventBus(synchronous=True)
    rec = RecordingBus(bus)
    stt = SpeechToText(bus, WhisperModelPool(tmp_path, lambda name, **kw: model), dict(STT_SETTINGS))
    stt.start()
    return stt, rec


def test_transcription_success_event(tmp_path):
    stt, rec = make_stt(tmp_path, FakeWhisperModel("open my tasks"))
    stt.transcribe_async(np.zeros(SAMPLE_RATE, np.float32), request_id=7).result(5)
    ready = rec.of(Events.TRANSCRIPT_READY)
    assert ready and ready[0]["text"] == "open my tasks" and ready[0]["request_id"] == 7
    assert stt.ready
    stt.stop()


def test_transcription_failure_is_reported_not_raised(tmp_path):
    stt, rec = make_stt(tmp_path, FakeWhisperModel(error=RuntimeError("CUDA missing")))
    stt.transcribe_async(np.zeros(SAMPLE_RATE, np.float32), request_id=1).result(5)
    failed = rec.of(Events.TRANSCRIPT_FAILED)
    assert failed and "CUDA missing" in failed[0]["error"] and not failed[0].get("empty")
    stt.stop()


def test_model_load_failure_marks_stt_unavailable(tmp_path):
    def broken_loader(name, **kw):
        raise OSError("download blocked")

    bus = EventBus(synchronous=True)
    rec = RecordingBus(bus)
    stt = SpeechToText(bus, WhisperModelPool(tmp_path, broken_loader), dict(STT_SETTINGS))
    stt.start()
    assert wait_for(lambda: any("load failed" in p["detail"] for p in rec.of(Events.STT_STATUS)))
    assert not stt.ready
    stt.stop()


def test_empty_and_hallucinated_output_counts_as_nothing_heard(tmp_path):
    for model in (FakeWhisperModel(""), FakeWhisperModel("Thank you."),
                  FakeWhisperModel("music", no_speech_prob=0.9, avg_logprob=-1.5)):
        stt, rec = make_stt(tmp_path, model)
        stt.transcribe_async(np.zeros(SAMPLE_RATE, np.float32)).result(5)
        failed = rec.of(Events.TRANSCRIPT_FAILED)
        assert failed and failed[0]["empty"], model.text
        stt.stop()


def test_very_short_audio_skips_model():
    model = FakeWhisperModel("x")
    assert transcribe_array(model, np.zeros(800, np.float32), "en").empty
    assert model.calls == 0


def test_disabled_stt(tmp_path):
    bus = EventBus(synchronous=True)
    rec = RecordingBus(bus)
    stt = SpeechToText(bus, WhisperModelPool(tmp_path), {**STT_SETTINGS, "enabled": False})
    stt.start()
    stt.transcribe_async(np.zeros(SAMPLE_RATE, np.float32)).result(5)
    assert "disabled" in rec.of(Events.TRANSCRIPT_FAILED)[0]["error"]
    stt.stop()


# ---------------------------------------------------------------- TTS
def test_sentence_splitting():
    assert split_sentences("Hello there. How are you? Fine!") == ["Hello there.", "How are you?", "Fine!"]
    long = "word " * 100
    assert all(len(p) <= 240 for p in split_sentences(long))


def make_tts(tmp_path, synth_factory=lambda p, s: FakeSynth(), voice_exists=True):
    FakeOutputStream.instances = []
    bus = EventBus(synchronous=True)
    rec = RecordingBus(bus)
    voice = tmp_path / "voice.onnx"
    player = AudioPlayer(stream_factory=FakeOutputStream)
    tts = TextToSpeech(bus, player, voice, synth_factory=synth_factory)
    tts.start()
    assert wait_for(lambda: tts.ready)  # the voice loads on the worker thread
    return tts, rec, player


def test_playback_lifecycle_events(tmp_path):
    tts, rec, player = make_tts(tmp_path)
    uid = tts.speak("Hello there. This is DUMBOO.")
    assert uid is not None
    assert wait_for(lambda: rec.of(Events.PLAYBACK_FINISHED))
    names = [n for n in rec.names() if n.startswith("playback.") and n != Events.PLAYBACK_LEVEL]
    assert names == [Events.PLAYBACK_STARTED, Events.PLAYBACK_FINISHED]
    finished = rec.of(Events.PLAYBACK_FINISHED)[0]
    assert finished == {"utterance_id": uid, "interrupted": False, "started": True}
    assert not player.active and player.samples_played > 0
    stream = FakeOutputStream.instances[-1]
    assert not stream.active  # speaker stream closed after speaking
    tts.stop()


def test_stop_interrupts_promptly(tmp_path):
    tts, rec, player = make_tts(tmp_path, lambda p, s: FakeSynth(seconds=20))
    uid = tts.speak("A very long answer.")
    assert wait_for(lambda: rec.of(Events.PLAYBACK_STARTED))
    tts.stop_speaking()
    assert wait_for(lambda: rec.of(Events.PLAYBACK_FINISHED), timeout=2)
    assert rec.of(Events.PLAYBACK_FINISHED)[0] == {"utterance_id": uid, "interrupted": True, "started": True}
    assert FakeOutputStream.instances[-1].aborted
    assert not player.active
    tts.stop()


def test_new_speech_never_overlaps_old(tmp_path):
    tts, rec, player = make_tts(tmp_path, lambda p, s: FakeSynth(seconds=5))
    first = tts.speak("first")
    assert wait_for(lambda: rec.of(Events.PLAYBACK_STARTED))
    second = tts.speak("second")
    assert wait_for(lambda: any(p["utterance_id"] == second for p in rec.of(Events.PLAYBACK_STARTED)))
    finished_first = [p for p in rec.of(Events.PLAYBACK_FINISHED) if p["utterance_id"] == first]
    assert finished_first and finished_first[0]["interrupted"]
    active = [s for s in FakeOutputStream.instances if s.active]
    assert len(active) == 1  # only one speaker stream at a time
    tts.stop()
    assert not any(s.active for s in FakeOutputStream.instances)


def test_missing_voice_model_reports_unavailable(tmp_path):
    bus = EventBus(synchronous=True)
    rec = RecordingBus(bus)
    tts = TextToSpeech(bus, AudioPlayer(stream_factory=FakeOutputStream), tmp_path / "nope.onnx")
    tts.start()
    assert wait_for(lambda: "not found" in tts.detail)
    assert not tts.ready
    assert "not found" in rec.of(Events.TTS_STATUS)[-1]["detail"]
    assert tts.speak("hello") is None
    tts.stop()


def test_synthesis_failure_still_finishes(tmp_path):
    class Broken(FakeSynth):
        def synthesize(self, text):
            raise RuntimeError("espeak crashed")
            yield  # pragma: no cover

    tts, rec, _ = make_tts(tmp_path, lambda p, s: Broken())
    uid = tts.speak("hi")
    assert wait_for(lambda: rec.of(Events.PLAYBACK_FINISHED))
    assert rec.of(Events.PLAYBACK_FINISHED)[0] == {"utterance_id": uid, "interrupted": True, "started": False}
    assert any(p["source"] == "tts" for p in rec.of(Events.ERROR))
    tts.stop()


PIPER_VOICE = Path(__file__).resolve().parent.parent / "models" / "piper" / "en_US-lessac-medium.onnx"


@pytest.mark.skipif(not PIPER_VOICE.exists(), reason="Piper voice not downloaded")
def test_real_piper_voice_synthesizes_audio():
    from audio.text_to_speech import PiperSynthesizer

    synth = PiperSynthesizer(PIPER_VOICE)
    audio = np.concatenate(list(synth.synthesize("Testing one two three.")))
    assert synth.sample_rate == 22050
    assert 0.8 < len(audio) / synth.sample_rate < 4
    assert np.abs(audio).max() > 0.05


def test_speech_level_keeps_flowing_while_the_next_sentence_is_synthesised(tmp_path):
    import time

    window = {}

    class SlowSecondSentence(FakeSynth):
        def synthesize(self, text):
            if self.spoken:                        # the second sentence takes 0.6 s to synthesise
                window["start"] = time.monotonic()
                time.sleep(0.6)
                window["end"] = time.monotonic()
            yield from super().synthesize(text)

    tts, rec, _player = make_tts(tmp_path, lambda p, s: SlowSecondSentence())
    stamps = []
    tts._bus.subscribe(Events.PLAYBACK_LEVEL, lambda _n, _p: stamps.append(time.monotonic()))
    tts.speak("First sentence. Second sentence.")
    assert wait_for(lambda: rec.of(Events.PLAYBACK_FINISHED), timeout=10)
    during = [t for t in stamps if window["start"] < t < window["end"]]
    assert len(during) >= 6        # ~20 Hz, so the blob keeps moving instead of freezing mid-answer
    tts.stop()
