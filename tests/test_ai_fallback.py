"""Multiple providers and keys: route order, automatic switching, cooldowns, Groq adapter."""

from types import SimpleNamespace

import pytest

from ai import errors as E
from ai.base import AIRequest, ChatMessage
from ai.config import AIConfig, api_keys_for
from ai.controller import ConversationController
from ai.factory import create_provider
from ai.fallback import FallbackProvider
from ai.providers.groq_provider import GroqProvider
from app.event_bus import EventBus, Events
from tests.ai_fakes import ScriptedProvider
from tests.fakes import FakeClock, RecordingBus

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
ENV = {"GROQ_API_KEY": "gsk_main", "GROQ_API_KEY_2": "gsk_extra", "GEMINI_API_KEY": "gem_key",
       "OPENAI_API_KEY": "your_openai_api_key_here"}  # a placeholder is not a key


def request(text="hi", timeout=30.0):
    return AIRequest(system_prompt="sys", messages=[ChatMessage("user", text)], timeout_s=timeout)


def chain(*providers, clock=None):
    """FallbackProvider over ready-made fake providers (route i -> providers[i])."""
    routes = [(p.name, p.model, f"key{i}") for i, p in enumerate(providers)]
    by_key = {f"key{i}": p for i, p in enumerate(providers)}
    return FallbackProvider(routes, lambda _p, _m, key: by_key[key], switch_timeout_s=8.0,
                            clock=clock or FakeClock())


def fake(name, **kw):
    p = ScriptedProvider(**kw, model=f"{name}-model")
    p.name = name
    return p


# -- configuration -------------------------------------------------------------------------------
def test_routes_use_every_key_in_order_and_skip_placeholders():
    config = AIConfig.from_settings({"provider": "groq"}, ENV)
    assert [(p, k) for p, _m, k in config.routes()] == [
        ("groq", "gsk_main"), ("groq", "gsk_extra"), ("gemini", "gem_key")]
    assert api_keys_for("openai", ENV) == []
    assert "switches to 1 extra key(s), gemini" in config.describe()
    assert "gsk_" not in config.describe()  # keys are never shown


def test_chosen_provider_comes_first_and_order_is_configurable():
    config = AIConfig.from_settings({"provider": "gemini", "fallback_order": "openai,groq"}, ENV)
    assert config.provider_order() == ["gemini", "openai", "groq"]
    assert [p for p, _m, _k in config.routes()] == ["gemini", "groq", "groq"]


def test_switching_off_uses_only_the_chosen_provider():
    config = AIConfig.from_settings({"provider": "groq", "auto_switch": False}, ENV)
    assert [(p, k) for p, _m, k in config.routes()] == [("groq", "gsk_main")]
    assert not isinstance(create_provider(config), FallbackProvider)


def test_missing_main_key_is_fine_when_another_provider_has_one():
    config = AIConfig.from_settings({"provider": "groq"}, {"GEMINI_API_KEY": "gem_key"})
    config.validate()
    assert [p for p, _m, _k in config.routes()] == ["gemini"]
    with pytest.raises(E.ConfigError):
        AIConfig.from_settings({"provider": "groq", "auto_switch": False}, {"GEMINI_API_KEY": "g"}).validate()


def test_factory_builds_a_switching_chain_for_several_keys():
    provider = create_provider(AIConfig.from_settings({"provider": "groq"}, ENV))
    assert isinstance(provider, FallbackProvider)
    assert [r.label for r in provider.routes] == ["Groq", "Groq (key 2)", "Gemini"]


# -- switching ----------------------------------------------------------------------------------------
def test_quota_problem_switches_and_the_route_rests():
    clock = FakeClock()
    groq = fake("groq", errors=[E.QuotaError(provider="groq")], answer="from groq")
    gemini = fake("gemini", answer="from gemini")
    provider = chain(groq, gemini, clock=clock)
    switches = []
    provider.on_switch = lambda a, b, kind: switches.append((a, b, kind))

    assert provider.generate_response(request()).text == "from gemini"
    assert switches == [("Groq", "Gemini", "quota")]
    assert (provider.name, provider.model) == ("gemini", "gemini-model")
    # The next message goes straight to the healthy route: no wait for the resting one.
    assert provider.generate_response(request()).text == "from gemini"
    assert len(groq.requests) == 1
    # After the rest, the preferred route is used again.
    clock.advance(1801)
    assert provider.generate_response(request()).text == "from groq"


def test_streaming_switches_before_any_text():
    groq = fake("groq", errors=[E.NetworkError(provider="groq")])
    gemini = fake("gemini", pieces=["Hello ", "there."])
    text = "".join(c.text for c in chain(groq, gemini).stream_response(request()))
    assert text == "Hello there."


def test_no_silent_restart_after_text_has_streamed():
    groq = fake("groq", pieces=["Half ", "an ", "answer"], errors=[E.NetworkError(provider="groq")], fail_after=1)
    gemini = fake("gemini", answer="other")
    seen = []
    with pytest.raises(E.NetworkError):
        for chunk in chain(groq, gemini).stream_response(request()):
            seen.append(chunk.text)
    assert seen == ["Half "] and gemini.requests == []


def test_safety_block_is_never_switched_around():
    groq = fake("groq", errors=[E.BlockedError(provider="groq")])
    gemini = fake("gemini", answer="sneaky")
    with pytest.raises(E.BlockedError):
        chain(groq, gemini).generate_response(request())
    assert gemini.requests == []


def test_request_the_model_cannot_take_moves_on_without_resting():
    clock = FakeClock()
    groq = fake("groq", errors=[E.BadRequestError("images not supported", provider="groq")], answer="text ok")
    gemini = fake("gemini", answer="I can see the screen")
    provider = chain(groq, gemini, clock=clock)
    assert provider.generate_response(request("what's on screen?")).text == "I can see the screen"
    assert provider.generate_response(request("hello")).text == "text ok"  # Groq not put to rest


def test_rate_limit_rest_follows_the_provider_hint():
    clock = FakeClock()
    groq = fake("groq", errors=[E.RateLimitError(provider="groq", retry_after=40)], answer="groq again")
    gemini = fake("gemini", answer="gemini")
    provider = chain(groq, gemini, clock=clock)
    provider.generate_response(request())
    clock.advance(30)
    assert provider.generate_response(request()).text == "gemini"
    clock.advance(15)
    assert provider.generate_response(request()).text == "groq again"


def test_all_routes_failing_reports_the_last_problem():
    groq = fake("groq", errors=[E.QuotaError(provider="groq")])
    gemini = fake("gemini", errors=[E.AuthError(provider="gemini")])
    with pytest.raises(E.AuthError):
        chain(groq, gemini).generate_response(request())


def test_when_everything_rests_the_soonest_route_is_still_tried():
    clock = FakeClock()
    groq = fake("groq", errors=[E.NetworkError(provider="groq")], answer="groq back")
    gemini = fake("gemini", errors=[E.QuotaError(provider="gemini")])
    provider = chain(groq, gemini, clock=clock)
    with pytest.raises(E.QuotaError):
        provider.generate_response(request())
    assert provider.generate_response(request()).text == "groq back"


def test_a_slow_provider_gets_a_shorter_timeout_when_a_backup_exists():
    groq, gemini = fake("groq", answer="a"), fake("gemini", answer="b")
    chain(groq, gemini).generate_response(request(timeout=30))
    assert groq.requests[0].timeout_s == 8.0


def test_controller_announces_the_switch_and_credits_the_answering_provider():
    bus = EventBus(synchronous=True)
    rec = RecordingBus(bus)
    groq = fake("groq", errors=[E.QuotaError(provider="groq")])
    gemini = fake("gemini", answer="Hello from Gemini.")
    provider = chain(groq, gemini)
    conversation = ConversationController(bus, AIConfig(provider="groq", api_key="k"), ROOT,
                                          provider_factory=lambda _c: provider, run_inline=True, clock=FakeClock())
    conversation.submit("hello", "typed", seq=1)
    switched = rec.of(Events.AI_SWITCHED)
    assert switched and switched[0]["from"] == "Groq" and switched[0]["to"] == "Gemini"
    assert switched[0]["reason"] == "quota used up"
    ready = rec.of(Events.RESPONSE_READY)[-1]
    assert ready["provider"] == "gemini" and ready["text"] == "Hello from Gemini."
    assert rec.of(Events.AI_STATUS)[-1]["provider"] == "gemini"


# -- Groq adapter -------------------------------------------------------------------------------------
class FakeCompletions:
    def __init__(self, pieces=("Hi", " there."), error=None):
        self.calls, self.pieces, self.error = [], pieces, error

    def create(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        if kw.get("stream"):
            chunks = [SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=p), finish_reason=None)])
                      for p in self.pieces]
            chunks.append(SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None),
                                                                   finish_reason="stop")]))
            return iter(chunks)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="".join(self.pieces)),
                                                        finish_reason="stop")])


def groq_with(completions):
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return GroqProvider("gsk_test", "openai/gpt-oss-120b", "low", client=client)


def test_groq_sends_a_quick_chat_request_and_streams_the_answer():
    completions = FakeCompletions()
    text = "".join(c.text for c in groq_with(completions).stream_response(request("hello")))
    assert text == "Hi there."
    sent = completions.calls[0]
    assert sent["messages"][0] == {"role": "system", "content": "sys"}
    assert sent["messages"][-1] == {"role": "user", "content": "hello"}
    # Hidden reasoning is never shown. Regression: as a plain argument the OpenAI SDK rejected it
    # (TypeError), so every Groq request failed; Groq-only options must go in extra_body.
    assert sent["reasoning_effort"] == "low" and sent["extra_body"] == {"include_reasoning": False}
    assert "include_reasoning" not in sent
    assert groq_with(FakeCompletions()).generate_response(request()).text == "Hi there."


def test_groq_errors_are_translated_and_name_groq():
    import httpx
    import openai

    response = httpx.Response(401, request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions"))
    err = openai.AuthenticationError("bad key", response=response, body={"error": {"message": "Invalid API Key"}})
    with pytest.raises(E.AuthError) as info:
        groq_with(FakeCompletions(error=err)).generate_response(request())
    assert "Groq" in info.value.user_message and "GROQ_API_KEY" in info.value.user_message
    assert info.value.provider == "groq"


# -- saving keys from the dashboard -------------------------------------------------------------------
def test_add_key_uses_the_next_free_slot_and_keeps_existing_keys(tmp_path):
    from dashboard.dashboard_controller import DashboardController
    from utils.env import read_env_file

    (tmp_path / ".env").write_text("GROQ_API_KEY=gsk_one\n", encoding="utf-8")
    environ = {"GROQ_API_KEY": "gsk_one"}
    reloads = []
    controller = DashboardController.__new__(DashboardController)
    controller.core = SimpleNamespace(paths=SimpleNamespace(root=tmp_path), environ=environ,
                                      reload_ai_keys=lambda: reloads.append(1))
    controller.save_api_key("groq", "gsk_two", extra=True)
    controller.save_api_key("openai", "sk-first", extra=True)  # no key yet: becomes the main key
    saved = read_env_file(tmp_path / ".env")
    assert saved["GROQ_API_KEY"] == "gsk_one" and saved["GROQ_API_KEY_2"] == "gsk_two"
    assert saved["OPENAI_API_KEY"] == "sk-first"
    with pytest.raises(ValueError):
        controller.save_api_key("groq", "gsk_two", extra=True)  # already saved
    controller.save_api_key("groq", "gsk_new")  # SAVE KEY replaces only the main key
    assert read_env_file(tmp_path / ".env")["GROQ_API_KEY_2"] == "gsk_two"
    assert len(reloads) == 3
