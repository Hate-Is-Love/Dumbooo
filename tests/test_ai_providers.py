"""Gemini and OpenAI adapters, using the REAL SDKs against a local fake server.

No network, no API key, no cost: the SDKs talk to ``tests/fake_ai_server.py``.
Live checks against the real services are in ``scripts/ai_check.py``.
"""

import socket

import pytest

from ai import errors as E
from ai.base import AIRequest, CancelToken, ChatMessage
from ai.providers.gemini_provider import GeminiProvider
from ai.providers.openai_provider import OpenAIProvider
from tests.fake_ai_server import (FakeAIServer, Reply, gemini_chunk, gemini_error, gemini_quota_details, gemini_text,
                                  openai_error, openai_response, openai_stream)

KEY_G = "AIzaFAKEKEYFORTESTS000000000000000000"
KEY_O = "sk-fake-test-key-0000000000000000"


@pytest.fixture
def server():
    s = FakeAIServer()
    yield s
    s.close()


def request(timeout=10.0, temperature=None):
    return AIRequest(system_prompt="Be brief.", messages=[ChatMessage("user", "What is RAM?"),
                                                          ChatMessage("assistant", "Temporary memory."),
                                                          ChatMessage("user", "Is it fast?")],
                     max_output_tokens=256, temperature=temperature, timeout_s=timeout)


def gemini(server, thinking="low"):
    return GeminiProvider(KEY_G, "gemini-test-flash", thinking, base_url=server.url + "/")


def openai_(server, effort="low"):
    return OpenAIProvider(KEY_O, "gpt-test", effort, base_url=server.url + "/v1")


def text_of(chunks):
    return "".join(c.text for c in chunks)


# ======================================================================= Gemini
def test_gemini_generate_sends_history_system_prompt_and_key_in_header(server):
    server.add(Reply(body=gemini_text("Yes, very fast.")))
    result = gemini(server).generate_response(request(temperature=0.3))
    assert result.text == "Yes, very fast." and result.finish_reason == "stop"
    sent = server.requests[0]
    assert sent["path"] == "/v1beta/models/gemini-test-flash:generateContent"
    assert sent["headers"]["x-goog-api-key"] == KEY_G and KEY_G not in sent["path"]
    body = sent["json"]
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    assert body["systemInstruction"]["parts"][0]["text"] == "Be brief."
    gen = body["generationConfig"]
    assert gen["maxOutputTokens"] == 256 and gen["temperature"] == 0.3
    assert "thinkingConfig" in gen


def test_gemini_omits_temperature_when_default(server):
    server.add(Reply(body=gemini_text("ok")))
    gemini(server).generate_response(request())
    assert "temperature" not in server.requests[0]["json"]["generationConfig"]


def test_gemini_streaming(server):
    server.add(Reply(sse=[(None, gemini_chunk("RAM is ")), (None, gemini_chunk("temporary memory.")),
                          (None, gemini_chunk(None, "STOP"))]))
    chunks = list(gemini(server).stream_response(request()))
    assert text_of(chunks) == "RAM is temporary memory."
    assert chunks[-1].finish_reason == "stop"
    assert server.requests[0]["path"].endswith(":streamGenerateContent?alt=sse")


def test_gemini_daily_free_tier_quota_is_quota_error(server):
    details = gemini_quota_details("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "40s")
    server.add(Reply(429, gemini_error(429, "RESOURCE_EXHAUSTED", "You exceeded your current quota, please check "
                                                                  "your plan and billing details.", details)))
    with pytest.raises(E.QuotaError) as info:
        gemini(server).generate_response(request())
    assert "daily" in info.value.user_message and not info.value.retryable
    assert KEY_G not in info.value.detail and KEY_G not in info.value.user_message


def test_gemini_per_minute_limit_is_rate_limit_with_retry_hint(server):
    details = gemini_quota_details("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "12s")
    server.add(Reply(429, gemini_error(429, "RESOURCE_EXHAUSTED", "Resource has been exhausted.", details)))
    with pytest.raises(E.RateLimitError) as info:
        gemini(server).generate_response(request())
    assert info.value.retry_after == 12 and info.value.retryable


def test_gemini_invalid_key_is_auth_error(server):
    server.add(Reply(400, gemini_error(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key.")))
    with pytest.raises(E.AuthError):
        gemini(server).generate_response(request())


def test_gemini_unknown_model(server):
    server.add(Reply(404, gemini_error(404, "NOT_FOUND", "models/gemini-test-flash is not found")))
    with pytest.raises(E.ModelNotFoundError) as info:
        gemini(server).generate_response(request())
    assert "gemini-test-flash" in info.value.user_message


def test_gemini_unsupported_thinking_is_dropped_and_retried_once(server):
    server.add(Reply(400, gemini_error(400, "INVALID_ARGUMENT", "Thinking level is not supported for this model.")),
               Reply(body=gemini_text("fine")))
    provider = gemini(server)
    assert provider.generate_response(request()).text == "fine"
    assert "thinkingConfig" not in server.requests[1]["json"].get("generationConfig", {})
    server.add(Reply(body=gemini_text("again")))
    provider.generate_response(request())  # remembered: not sent again
    assert len(server.requests) == 3 and "thinkingConfig" not in server.requests[2]["json"]["generationConfig"]


def test_gemini_overloaded_is_retryable(server):
    server.add(Reply(503, gemini_error(503, "UNAVAILABLE", "The model is overloaded.")))
    with pytest.raises(E.ProviderUnavailableError) as info:
        gemini(server).generate_response(request())
    assert info.value.retryable


def test_gemini_timeout(server):
    server.add(Reply(body=gemini_text("late"), delay_s=2.5))
    with pytest.raises(E.TimeoutError_):
        gemini(server).generate_response(request(timeout=1.0))


def test_gemini_network_failure():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # nothing listens here after the socket closes
    provider = GeminiProvider(KEY_G, "m", base_url=f"http://127.0.0.1:{port}/")
    with pytest.raises(E.NetworkError):
        provider.generate_response(request(timeout=3))


def test_gemini_empty_and_truncated_and_blocked(server):
    server.add(Reply(body=gemini_text("", "STOP")))
    with pytest.raises(E.EmptyResponseError):
        gemini(server).generate_response(request())
    server.add(Reply(body=gemini_text("Partial answ", "MAX_TOKENS")))
    assert gemini(server).generate_response(request()).truncated
    server.add(Reply(body={"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []}))
    with pytest.raises(E.BlockedError):
        gemini(server).generate_response(request())


def test_gemini_stream_cancellation(server):
    server.add(Reply(sse=[(None, gemini_chunk(f"part{i} ")) for i in range(30)], sse_gap_s=0.02))
    cancel = CancelToken()
    got = []
    with pytest.raises(E.CancelledError):
        for chunk in gemini(server).stream_response(request(), cancel):
            got.append(chunk.text)
            if len(got) == 3:
                cancel.cancel()
    assert len(got) == 3


def test_gemini_health_check_uses_models_get(server):
    server.add(Reply(body={"name": "models/gemini-test-flash", "displayName": "Gemini Test Flash",
                           "outputTokenLimit": 8192}))
    status = gemini(server).health_check()
    assert status.ok and "Gemini Test Flash" in status.message
    assert server.requests[0]["method"] == "GET"
    server.add(Reply(400, gemini_error(400, "INVALID_ARGUMENT", "API key not valid.")))
    bad = gemini(server).health_check()
    assert not bad.ok and "rejected" in bad.message


# ======================================================================= OpenAI
def test_openai_generate_request_shape(server):
    server.add(Reply(body=openai_response("Yes.")))
    result = openai_(server).generate_response(request(temperature=0.2))
    assert result.text == "Yes."
    sent = server.requests[0]
    assert sent["path"] == "/v1/responses" and sent["headers"]["authorization"] == f"Bearer {KEY_O}"
    body = sent["json"]
    assert body["instructions"] == "Be brief." and body["store"] is False
    assert [m["role"] for m in body["input"]] == ["user", "assistant", "user"]
    assert body["max_output_tokens"] == 256 and body["temperature"] == 0.2
    assert body["reasoning"] == {"effort": "low"}


def test_openai_streaming(server):
    server.add(Reply(sse=openai_stream(["120", "."])))
    chunks = list(openai_(server).stream_response(request()))
    assert text_of(chunks) == "120." and chunks[-1].finish_reason == "stop"
    assert server.requests[0]["json"]["stream"] is True


def test_openai_insufficient_quota_is_quota_not_rate_limit(server):
    server.add(Reply(429, openai_error("You exceeded your current quota, please check your plan and billing details.",
                                       "insufficient_quota", "insufficient_quota")))
    with pytest.raises(E.QuotaError) as info:
        openai_(server).generate_response(request())
    assert "ChatGPT subscription does not include API credit" in info.value.user_message


def test_openai_rate_limit_with_retry_after(server):
    server.add(Reply(429, openai_error("Rate limit reached", "requests", "rate_limit_exceeded"),
                     headers={"retry-after": "3"}))
    with pytest.raises(E.RateLimitError) as info:
        openai_(server).generate_response(request())
    assert info.value.retry_after == 3


def test_openai_auth_and_model_errors(server):
    server.add(Reply(401, openai_error("Incorrect API key provided", "invalid_request_error", "invalid_api_key")))
    with pytest.raises(E.AuthError):
        openai_(server).generate_response(request())
    server.add(Reply(404, openai_error("The model `gpt-test` does not exist", "invalid_request_error",
                                       "model_not_found")))
    with pytest.raises(E.ModelNotFoundError):
        openai_(server).generate_response(request())


def test_openai_unsupported_temperature_and_reasoning_are_dropped(server):
    server.add(Reply(400, openai_error("Unsupported parameter: 'temperature' is not supported with this model.",
                                       "invalid_request_error", "unsupported_parameter", "temperature")),
               Reply(400, openai_error("Unsupported parameter: 'reasoning.effort' is not supported with this model.",
                                       "invalid_request_error", "unsupported_parameter", "reasoning.effort")),
               Reply(body=openai_response("ok")))
    assert openai_(server).generate_response(request(temperature=0.5)).text == "ok"
    last = server.requests[-1]["json"]
    assert "temperature" not in last and "reasoning" not in last


def test_openai_incomplete_max_tokens_and_empty(server):
    server.add(Reply(body=openai_response("Partial", status="incomplete", incomplete_reason="max_output_tokens")))
    assert openai_(server).generate_response(request()).truncated
    server.add(Reply(body=openai_response("", status="incomplete", incomplete_reason="max_output_tokens")))
    with pytest.raises(E.EmptyResponseError) as info:
        openai_(server).generate_response(request())
    assert "Max output tokens" in info.value.user_message


def test_openai_server_error_and_timeout(server):
    server.add(Reply(500, openai_error("server error", "server_error")))
    with pytest.raises(E.ProviderUnavailableError):
        openai_(server).generate_response(request())
    server.add(Reply(body=openai_response("late"), delay_s=2.5))
    with pytest.raises(E.TimeoutError_):
        openai_(server).generate_response(request(timeout=1.0))


def test_openai_stream_cancel_from_other_thread_closes_stream(server):
    import threading

    server.add(Reply(sse=openai_stream([f"w{i} " for i in range(40)]), sse_gap_s=0.03))
    provider = openai_(server)
    cancel = CancelToken()
    got = []

    def stop_soon():
        import time
        time.sleep(0.2)
        cancel.cancel()
        provider.cancel_generation()

    threading.Thread(target=stop_soon).start()
    with pytest.raises(E.CancelledError):
        for chunk in provider.stream_response(request(), cancel):
            got.append(chunk.text)
    assert 0 < len(got) < 40


def test_openai_health_check(server):
    server.add(Reply(body={"id": "gpt-test", "object": "model", "created": 0, "owned_by": "openai"}))
    assert openai_(server).health_check().ok
    assert server.requests[0]["path"] == "/v1/models/gpt-test"
