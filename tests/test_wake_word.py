"""Wake-phrase matching, engine selection and duplicate-wake prevention."""

from pathlib import Path

import numpy as np
import pytest

from audio.speech_to_text import WhisperModelPool
from audio.wake_word import (OpenWakeWordEngine, WakeWordService, WhisperSpotterEngine, match_phrase,
                             spotter_threshold)
from tests.fakes import FakeClock, FakeWhisperModel, wait_for

SETTINGS = {"enabled": True, "phrase": "hey dumboo", "engine": "auto", "model_path": "x.onnx",
            "spotter_model": "tiny.en", "sensitivity": 0.5, "cooldown_s": 2.0}


@pytest.mark.parametrize("text", ["Hey Dumboo.", "hey dumbo", "Hi, Dumbo!", "Hey, Dum boo.", "hey dumboo what time is it"])
def test_phrase_variants_match(text):
    m = match_phrase(text, "hey dumboo")
    assert m is not None and m.score >= spotter_threshold(0.5)


@pytest.mark.parametrize("text", ["Hey Jumbo", "Hey there", "I watched Dumbo yesterday with my family",
                                  "turn on the lights", "", "hey", "Hey dumb dumb.", "Dumb old boo."])
def test_other_speech_does_not_match(text):
    m = match_phrase(text, "hey dumboo")
    assert m is None or m.score < spotter_threshold(0.5)


def test_followup_command_is_extracted():
    m = match_phrase("Hey Dumbo, what's on my schedule?", "hey dumboo")
    assert m.followup == "what's on my schedule"


def test_sensitivity_changes_threshold():
    assert spotter_threshold(0.9) < spotter_threshold(0.1)


class FakeEngine:
    engine = "fake"
    detail = "fake engine"

    def __init__(self, on_detect):
        self.on_detect = on_detect
        self.frames = 0
        self.resets = 0
        self.closed = False

    def process(self, raw, clean, prob):
        self.frames += 1

    def reset(self):
        self.resets += 1

    def close(self):
        self.closed = True


def make_service(clock=None, **overrides):
    wakes = []
    engines = []

    def factory(cb):
        engines.append(FakeEngine(cb))
        return engines[-1]

    settings = {**SETTINGS, **overrides}
    svc = WakeWordService(settings, Path("missing.onnx"), WhisperModelPool(Path(".")), 0.5,
                          lambda e, s, f: wakes.append((e, s, f)), clock=clock or FakeClock(), engine_factory=factory)
    return svc, wakes, engines


def test_one_wake_event_per_detection_until_rearmed():
    clock = FakeClock()
    svc, wakes, engines = make_service(clock)
    svc.start()
    engines[0].on_detect(0.9, "")
    engines[0].on_detect(0.9, "")  # same phrase reported twice
    assert len(wakes) == 1 and not svc.armed
    svc.process(np.zeros(512), np.zeros(512), 0.0)
    assert engines[0].frames == 0  # disarmed: audio is not even analysed
    clock.advance(5)
    svc.arm()
    engines[0].on_detect(0.9, "")
    assert len(wakes) == 2


def test_cooldown_blocks_rapid_rewake():
    clock = FakeClock()
    svc, wakes, engines = make_service(clock, cooldown_s=2.0)
    svc.start()
    engines[0].on_detect(0.9, "")
    svc.arm()
    clock.advance(0.5)
    engines[0].on_detect(0.9, "")
    assert len(wakes) == 1


def test_start_twice_builds_one_engine():
    svc, _, engines = make_service()
    svc.start()
    svc.start()
    assert len(engines) == 1 and svc.loaded


def test_disabled_service_loads_nothing():
    svc, _, engines = make_service(enabled=False)
    svc.start()
    assert engines == [] and not svc.loaded and svc.detail == "disabled in settings"


def test_auto_selects_spotter_without_custom_model(tmp_path):
    loads = []

    def loader(name, **kw):
        loads.append(name)
        return FakeWhisperModel("hey dumbo")

    svc = WakeWordService(dict(SETTINGS), tmp_path / "missing.onnx", WhisperModelPool(tmp_path, loader), 0.5,
                          lambda *a: None)
    svc.start()
    assert svc.engine_name == "whisper_spotter"
    svc.stop()


def test_auto_selects_openwakeword_when_model_exists(tmp_path, monkeypatch):
    model = tmp_path / "hey_dumboo.onnx"
    model.write_bytes(b"fake")
    created = {}

    class FakeOWW:
        def __init__(self, **kw):
            created.update(kw)

    monkeypatch.setattr("openwakeword.model.Model", FakeOWW, raising=False)
    svc = WakeWordService(dict(SETTINGS), model, WhisperModelPool(tmp_path), 0.5, lambda *a: None)
    svc.start()
    assert svc.engine_name == "openwakeword"
    assert created["wakeword_models"] == [str(model)]


def test_openwakeword_engine_thresholds_and_chunks(tmp_path):
    model = tmp_path / "m.onnx"
    model.write_bytes(b"x")
    scores = iter([0.1, 0.2, 0.8, 0.1])
    fed = []

    class FakeOWW:
        def __init__(self, **kw):
            pass

        def predict(self, chunk):
            fed.append(len(chunk))
            return {"hey_dumboo": next(scores)}

        def reset(self):
            pass

    hits = []
    eng = OpenWakeWordEngine(model, 0.5, lambda s, f: hits.append(s), model_factory=FakeOWW)
    for _ in range(10):  # 10 x 512 samples = 4 chunks of 1280
        eng.process(np.zeros(512, np.float32), None, 0.0)
    assert fed == [1280] * 4
    assert hits == [0.8]


def test_whisper_spotter_detects_from_short_speech_burst(tmp_path):
    pool = WhisperModelPool(tmp_path, lambda name, **kw: FakeWhisperModel("Hey Dumbo."))
    hits = []
    eng = WhisperSpotterEngine(pool, "tiny.en", "hey dumboo", 0.5, 0.5, lambda s, f: hits.append(s), preload=False)
    loud, quiet = np.full(512, 0.1, np.float32), np.zeros(512, np.float32)
    for _ in range(25):
        eng.process(loud, loud, 0.95)  # ~0.8 s of speech
    for _ in range(20):
        eng.process(quiet, quiet, 0.0)
    assert wait_for(lambda: hits)
    eng.close()


LOUD, QUIET = np.full(512, 0.1, np.float32), np.zeros(512, np.float32)


def _spotter(tmp_path, model, hits):
    pool = WhisperModelPool(tmp_path, lambda name, **kw: model)
    return WhisperSpotterEngine(pool, "tiny.en", "hey dumboo", 0.5, 0.5, lambda s, f: hits.append((s, f)),
                                preload=False)


def _speak(eng, frames):
    for _ in range(frames):
        eng.process(LOUD, LOUD, 0.95)
    for _ in range(20):
        eng.process(QUIET, QUIET, 0.0)


def test_wake_phrase_followed_by_a_long_command_is_detected(tmp_path):
    # Regression: "Hey DUMBOO, open Notepad and type ..." in one breath (> 4 s) was thrown away.
    model, hits = FakeWhisperModel("Hey Dumbo, open Notepad and type a short note about the meeting"), []
    eng = _spotter(tmp_path, model, hits)
    _speak(eng, 190)  # ~6 s of continuous speech
    assert wait_for(lambda: hits)
    assert hits[0][1] == "open notepad and type a short note about the meeting"
    eng.close()


def test_very_long_speech_wakes_without_a_cut_off_command(tmp_path):
    model, hits = FakeWhisperModel("Hey Dumbo, tell me everything about"), []
    eng = _spotter(tmp_path, model, hits)
    _speak(eng, 340)  # > 10 s: the burst is cut, so the half command is not used
    assert wait_for(lambda: hits)
    assert hits[0][1] == ""
    eng.close()


def test_long_speech_without_the_phrase_is_checked_once_and_ignored(tmp_path):
    model, hits = FakeWhisperModel("so we were talking about the movie yesterday"), []
    eng = _spotter(tmp_path, model, hits)
    _speak(eng, 200)
    assert wait_for(lambda: model.calls >= 2)
    eng.close()
    assert hits == [] and model.calls == 2  # plain + hinted look at the first 2.5 s only


def test_name_hint_rescues_a_misheard_wake_phrase(tmp_path):
    model, hits = FakeWhisperModel("Hey dumpling.", hinted_text="Hey Dumboo.", hinted_word_prob=0.5), []
    eng = _spotter(tmp_path, model, hits)
    _speak(eng, 30)
    assert wait_for(lambda: hits)
    assert model.prompts == [None, "Hey Dumboo."]
    # Regression: random re-decoding (temperature fallback) made the same phrase wake only sometimes.
    assert all(k["temperature"] == 0.0 for k in model.kwargs)
    eng.close()


def test_name_hint_is_not_trusted_when_whisper_is_unsure(tmp_path):
    # Whisper writes the hinted name for sounds that only resemble it, but with very low probability.
    model, hits = FakeWhisperModel("Hey Dumbledore.", hinted_text="Hey Dumboo.", hinted_word_prob=0.03), []
    eng = _spotter(tmp_path, model, hits)
    _speak(eng, 30)
    assert wait_for(lambda: model.calls >= 2)
    eng.close()
    assert hits == []


def test_a_clearly_heard_similar_word_beats_the_hint(tmp_path):
    model, hits = FakeWhisperModel("Hey Jumbo!", word_prob=0.8, hinted_text="Hey Dumbo.", hinted_word_prob=0.4), []
    eng = _spotter(tmp_path, model, hits)
    _speak(eng, 30)
    assert wait_for(lambda: model.calls >= 2)
    eng.close()
    assert hits == []


def test_a_burst_during_transcription_is_checked_afterwards(tmp_path):
    # Regression: a wake phrase said while the previous sound was being transcribed was dropped.
    import threading
    gate = threading.Event()

    class Slow(FakeWhisperModel):
        def transcribe(self, audio, **kwargs):
            gate.wait(5)
            return super().transcribe(audio, **kwargs)

    model, hits = Slow("Hey Dumbo."), []
    eng = _spotter(tmp_path, model, hits)
    _speak(eng, 30)   # first burst: being transcribed (blocked)
    _speak(eng, 30)   # second burst arrives meanwhile
    gate.set()
    assert wait_for(lambda: len(hits) == 2)
    eng.close()
