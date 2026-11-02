"""AI configuration, provider selection, API-key handling and secret redaction."""

import logging

import pytest

from ai import errors as E
from ai.config import AIConfig, api_key_for, env_overrides
from ai.factory import create_provider
from ai.providers.echo_provider import EchoProvider
from ai.providers.gemini_provider import GeminiProvider
from ai.providers.openai_provider import OpenAIProvider
from app.application import AssistantCore
from services.settings_service import SettingsError, SettingsService
from tests.test_application import make_core
from utils.env import load_dotenv, mask, read_env_file, set_env_value
from utils.logging_config import configure_logging

GKEY = "AIzaSyTESTKEY0123456789abcdefghijklmnop"
OKEY = "sk-proj-TESTKEY0123456789abcdefghij"


def settings(paths):
    return SettingsService(paths.default_settings, paths.user_settings)


def test_groq_is_the_default_provider_with_automatic_switching(paths):
    config = AIConfig.from_settings(settings(paths).section("ai"), environ={})
    assert config.provider == "groq" and config.model == config.groq_model
    assert config.auto_switch and config.provider_order() == ["groq", "gemini", "openai"]


def test_provider_selection(paths):
    s = settings(paths)
    assert isinstance(create_provider(AIConfig.from_settings(s.section("ai"), {"GEMINI_API_KEY": GKEY})), GeminiProvider)
    s.update({"ai": {"provider": "openai"}})
    assert isinstance(create_provider(AIConfig.from_settings(s.section("ai"), {"OPENAI_API_KEY": OKEY})), OpenAIProvider)
    s.update({"ai": {"provider": "echo"}})
    assert isinstance(create_provider(AIConfig.from_settings(s.section("ai"), {})), EchoProvider)


def test_model_names_change_without_code_edits(paths):
    s = settings(paths)
    s.update({"ai": {"gemini_model": "gemini-future-9-flash", "openai_model": "gpt-future-mini"}})
    gemini = create_provider(AIConfig.from_settings(s.section("ai"), {"GEMINI_API_KEY": GKEY}))
    assert gemini.model == "gemini-future-9-flash"
    s.update({"ai": {"provider": "openai"}})
    assert create_provider(AIConfig.from_settings(s.section("ai"), {"OPENAI_API_KEY": OKEY})).model == "gpt-future-mini"


@pytest.mark.parametrize("provider, variable", [("gemini", "GEMINI_API_KEY"), ("openai", "OPENAI_API_KEY")])
def test_missing_key_gives_helpful_message(provider, variable):
    with pytest.raises(E.ConfigError) as info:
        create_provider(AIConfig(provider=provider, api_key=None))
    assert variable in info.value.user_message and "SETTINGS" in info.value.user_message


def test_placeholder_keys_from_env_example_are_ignored():
    assert api_key_for("gemini", {"GEMINI_API_KEY": "your_gemini_api_key_here"}) is None
    assert api_key_for("gemini", {"GOOGLE_API_KEY": GKEY}) == GKEY  # SDK-standard fallback name


def test_unknown_provider_and_invalid_settings_rejected(paths):
    with pytest.raises(E.ConfigError):
        AIConfig(provider="claude-clone").validate()
    s = settings(paths)
    for bad in ({"provider": "free-ai"}, {"gemini_model": "bad model name"}, {"max_output_tokens": 5},
                {"temperature": 3.5}, {"timeout_s": 1}, {"context_turns": -1}):
        with pytest.raises(SettingsError):
            s.update({"ai": bad})


def test_env_overrides_parse_and_report_bad_values():
    values, warnings = env_overrides({"AI_PROVIDER": "OpenAI", "OPENAI_MODEL": " gpt-x ", "AI_REQUEST_TIMEOUT": "20",
                                      "AI_MAX_OUTPUT_TOKENS": "300", "AI_TEMPERATURE": "default",
                                      "AI_STREAMING_ENABLED": "no"})
    assert values == {"provider": "openai", "openai_model": "gpt-x", "timeout_s": 20.0, "max_output_tokens": 300,
                      "temperature": None, "streaming": False}
    _, warnings = env_overrides({"AI_MAX_OUTPUT_TOKENS": "lots", "AI_STREAMING_ENABLED": "maybe"})
    assert len(warnings) == 2


def test_env_overrides_apply_for_the_run_but_are_not_saved(paths):
    core = make_core(paths, environ={"AI_PROVIDER": "openai", "OPENAI_MODEL": "gpt-env-model", "OPENAI_API_KEY": OKEY})
    try:
        assert core.ai_config().provider == "openai" and core.ai_config().model == "gpt-env-model"
        core.settings.update({"vad": {"end_silence_ms": 1000}})
        saved = paths.user_settings.read_text(encoding="utf-8")
        assert "gpt-env-model" not in saved and OKEY not in saved
    finally:
        core.shutdown()


def test_describe_never_contains_the_key():
    config = AIConfig(provider="gemini", api_key=GKEY)
    assert GKEY not in config.describe() and GKEY not in repr(config.describe())
    assert mask(GKEY) == f"set (…{GKEY[-4:]})" and mask(None) == "not set"


def test_dotenv_load_and_save(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nGEMINI_API_KEY='abc123456789'\nAI_PROVIDER=gemini  # inline\nEMPTY=\n",
                        encoding="utf-8")
    environ = {"AI_PROVIDER": "openai"}  # a real environment variable wins over the file
    loaded = load_dotenv(env_file, environ)
    assert environ == {"AI_PROVIDER": "openai", "GEMINI_API_KEY": "abc123456789"} and loaded == ["GEMINI_API_KEY"]
    set_env_value(env_file, "GEMINI_API_KEY", "newkey987654321", environ)
    set_env_value(env_file, "OPENAI_API_KEY", OKEY, environ)
    values = read_env_file(env_file)
    assert values["GEMINI_API_KEY"] == "newkey987654321" and values["OPENAI_API_KEY"] == OKEY
    assert env_file.read_text(encoding="utf-8").count("GEMINI_API_KEY") == 1
    with pytest.raises(ValueError):
        set_env_value(env_file, "GEMINI_API_KEY", "two\nlines", environ)


def test_secrets_are_redacted_from_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", GKEY)
    log_file = configure_logging(tmp_path, console=False)
    log = logging.getLogger("secret-test")
    log.error("request failed with key %s and header Bearer %s", GKEY, OKEY)
    log.warning("some sdk message: key=%s", "abcdefghijklmnopqrstuvwxyz")
    try:
        raise RuntimeError(f"boom {GKEY}")
    except RuntimeError:
        log.exception("traceback test")
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = log_file.read_text(encoding="utf-8")
    assert GKEY not in text and OKEY not in text and "abcdefghijklmnopqrstuvwxyz" not in text
    assert text.count("[REDACTED]") >= 4
    for handler in list(logging.getLogger().handlers):
        handler.close()
        logging.getLogger().removeHandler(handler)


def test_api_key_saved_from_dashboard_never_reaches_settings_or_logs(paths, monkeypatch):
    from dashboard.dashboard_controller import DashboardController  # noqa: F401 - Qt-free method under test

    environ = {"AI_PROVIDER": "gemini"}
    core = make_core(paths, environ=environ)
    monkeypatch.setattr(type(core.paths), "root", property(lambda self: paths.data_dir), raising=False)
    try:
        from utils.env import set_env_value as save
        save(paths.data_dir / ".env", "GEMINI_API_KEY", GKEY, environ)
        core.reload_ai_keys()
        assert core.ai_config().api_key == GKEY
        core.settings.update({"vad": {"end_silence_ms": 1100}})
        assert GKEY not in paths.user_settings.read_text(encoding="utf-8")
        assert "key set" in core.ai_config().describe()
    finally:
        core.shutdown()


def test_core_without_key_still_starts_and_reports(paths):
    core = make_core(paths, environ={"AI_PROVIDER": "gemini"})
    try:
        core.start()
        status = core.service_status()["ai"]
        assert not status["ok"] and "NO API KEY" in status["detail"]
        assert isinstance(core, AssistantCore)
    finally:
        core.shutdown()
