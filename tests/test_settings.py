import json

import pytest

from services.settings_service import SettingsError, SettingsService, validate


def make(paths, on_change=None):
    return SettingsService(paths.default_settings, paths.user_settings, on_change)


def test_shipped_defaults_are_valid(paths):
    with open(paths.default_settings, encoding="utf-8") as fh:
        assert validate(json.load(fh)) == []


def test_loads_defaults_when_no_user_file(paths):
    s = make(paths)
    assert s.get("wake_word", "phrase") == "hey dumboo"
    assert s.get("dashboard", "start_mode") == "sleep"
    assert s.get("ai", "provider") == "groq"  # fastest first; switches to Gemini/OpenAI automatically


def test_update_persists_and_reports_changes(paths):
    calls = []
    s = make(paths, lambda snap, changed: calls.append(changed))
    changed = s.update({"vad": {"end_silence_ms": 1200}, "dashboard": {"auto_sleep": False}})
    assert sorted(changed) == ["dashboard.auto_sleep", "vad.end_silence_ms"]
    assert calls == [changed]
    reloaded = make(paths)
    assert reloaded.get("vad", "end_silence_ms") == 1200
    assert reloaded.get("dashboard", "auto_sleep") is False


def test_no_change_returns_empty_and_does_not_notify(paths):
    calls = []
    s = make(paths, lambda *a: calls.append(a))
    assert s.update({"vad": {"threshold": s.get("vad", "threshold")}}) == []
    assert calls == []


@pytest.mark.parametrize("changes", [
    {"vad": {"threshold": 5}},
    {"vad": {"end_silence_ms": "fast"}},
    {"dashboard": {"start_mode": "fullscreen"}},
    {"wake_word": {"phrase": ""}},
    {"wake_word": {"phrase": "one two three four five"}},
    {"stt": {"model": "gpt"}},
    {"dashboard": {"auto_sleep": 1}},
    {"tts": {"speed": True}},
    {"nonsense": {"x": 1}},
    {"vad": {"unknown_key": 1}},
    {"vad": {"min_speech_ms": 1900, "max_record_s": 1.5}},
])
def test_invalid_values_are_rejected_atomically(paths, changes):
    s = make(paths)
    before = s.snapshot()
    with pytest.raises(SettingsError):
        s.update(changes)
    assert s.snapshot() == before
    assert not paths.user_settings.exists()


def test_corrupt_user_file_falls_back_to_defaults(paths):
    paths.user_settings.write_text("{ not json", encoding="utf-8")
    s = make(paths)
    assert s.get("vad", "threshold") == 0.5


def test_invalid_saved_values_are_ignored_individually(paths):
    paths.user_settings.write_text(json.dumps({"vad": {"threshold": 99, "end_silence_ms": 700}}), encoding="utf-8")
    s = make(paths)
    assert s.get("vad", "threshold") == 0.5
    assert s.get("vad", "end_silence_ms") == 700


def test_session_override_is_not_saved(paths):
    s = make(paths)
    s.override_for_session("dashboard", "text_only", True)
    assert s.get("dashboard", "text_only") is True
    s.update({"vad": {"end_silence_ms": 800}})
    saved = json.loads(paths.user_settings.read_text(encoding="utf-8"))
    assert saved["dashboard"]["text_only"] is False
    assert saved["vad"]["end_silence_ms"] == 800


def test_reset_to_defaults(paths):
    s = make(paths)
    s.update({"tts": {"speed": 1.5}})
    s.reset_to_defaults()
    assert s.get("tts", "speed") == 1.0


def test_snapshot_is_a_copy(paths):
    s = make(paths)
    snap = s.snapshot()
    snap["vad"]["threshold"] = 0.9
    assert s.get("vad", "threshold") == 0.5
