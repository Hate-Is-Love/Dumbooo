"""ConversationController: context, prompts, duplicates, cancellation, retries, errors, switching."""

import threading
from pathlib import Path

import pytest

from ai import errors as E
from ai.config import AIConfig
from ai.context import MAX_USER_CHARS, ConversationContext, shorten_user_message
from ai.controller import ConversationController
from ai.prompts import TEXT_NOTE, VOICE_NOTE, build_system_prompt, load_base_prompt
from ai.speech_format import CODE_NOTE, SpeechChunker, to_speech
from app.event_bus import EventBus, Events
from tests.ai_fakes import ScriptedProvider
from tests.fakes import FakeClock, RecordingBus, wait_for

ROOT = Path(__file__).resolve().parent.parent


def make(provider=None, inline=True, **config):
    bus = EventBus(synchronous=True)
    rec = RecordingBus(bus)
    clock = FakeClock()
    provider = provider or ScriptedProvider()
    built = []

    def factory(cfg):
        built.append(cfg)
        return provider

    ctl = ConversationController(bus, AIConfig(provider="echo", **config), ROOT, provider_factory=factory,
                                 run_inline=inline, clock=clock)
    return ctl, rec, provider, clock, built


# ---------------------------------------------------------------- context
def test_context_keeps_newest_complete_turns_within_limits():
    ctx = ConversationContext(max_turns=2, max_chars=10_000)
    for i in range(5):
        ctx.add_turn(f"q{i}", f"a{i}", "typed")
    assert [m.content for m in ctx.messages_for("new")] == ["q3", "a3", "q4", "a4", "new"]


def test_context_char_budget_drops_oldest_first_and_never_splits_a_turn():
    ctx = ConversationContext(max_turns=10, max_chars=100)
    ctx.add_turn("x" * 40, "y" * 40, "typed")  # 80 chars
    ctx.add_turn("short", "reply", "voice")      # 10 chars
    msgs = ctx.messages_for("q" * 15)
    assert [m.content for m in msgs] == ["short", "reply", "q" * 15]
    assert ConversationContext().messages_for("hi")[0].content == "hi"  # empty conversation


def test_long_user_message_is_shortened_with_note():
    text, cut = shorten_user_message("a" * (MAX_USER_CHARS + 500))
    assert cut and text.startswith("a" * MAX_USER_CHARS) and "500 characters removed" in text


def test_system_prompt_is_concise_and_mode_aware():
    base = load_base_prompt(ROOT / "config" / "system_prompt.txt")
    assert "one to three short sentences" in base
    voice = build_system_prompt(base, concise=True, source="voice", voice_output=True)
    typed = build_system_prompt(base, concise=False, source="typed", voice_output=False)
    assert VOICE_NOTE in voice and TEXT_NOTE not in voice
    assert TEXT_NOTE in typed and "Concise mode is OFF" in typed
    assert load_base_prompt(ROOT / "missing.txt")  # falls back to the built-in prompt


def test_system_prompt_sent_once_not_in_history():
    ctl, _rec, provider, *_ = make()
    ctl.submit("one", "typed", 1)
    ctl.submit("two", "typed", 2)
    request = provider.requests[-1]
    assert all("DUMBOO" not in m.content for m in request.messages)
    assert "DUMBOO" in request.system_prompt


# ---------------------------------------------------------------- submissions
def test_text_and_voice_share_one_context():
    ctl, rec, provider, *_ = make()
    ctl.submit("Tell me about Python", "typed", 1)
    ctl.submit("What are its advantages?", "voice", 2)
    assert [m.content for m in provider.requests[-1].messages] == [
        "Tell me about Python", "Answer to: Tell me about Python.", "What are its advantages?"]
    assert rec.of(Events.RESPONSE_READY)[0]["elapsed"] >= 0
    assert [p["source"] for p in rec.of(Events.AI_STARTED)] == ["typed", "voice"]


def test_streaming_events_then_ready():
    ctl, rec, *_ = make()
    ctl.submit("hi", "typed", 7)
    deltas = rec.of(Events.RESPONSE_DELTA)
    assert deltas and all(d["seq"] == 7 for d in deltas) and deltas[-1]["text"].strip() == "Answer to: hi."
    ready = rec.of(Events.RESPONSE_READY)[0]
    assert ready["seq"] == 7 and ready["text"] == "Answer to: hi." and ready["speech"] == "Answer to: hi."


def test_duplicates_ignored_within_window_but_not_across_sources():
    ctl, rec, _p, clock, _ = make()
    assert ctl.submit("hello", "typed", 1)
    assert not ctl.submit("  Hello ", "typed", 2)
    assert ctl.submit("hello", "voice", 3)
    clock.advance(4)
    assert ctl.submit("hello", "typed", 4)
    assert not ctl.submit("", "typed", 5)
    assert len(rec.of(Events.AI_STARTED)) == 3


def test_new_message_cancels_running_one_and_old_tokens_never_mix():
    gate = threading.Event()
    ctl, rec, provider, *_ = make(ScriptedProvider(gate=gate), inline=False)
    ctl.submit("first", "typed", 1)
    ctl.submit("second", "typed", 2)
    gate.set()
    assert wait_for(lambda: rec.of(Events.RESPONSE_READY))
    assert {d["seq"] for d in rec.of(Events.RESPONSE_DELTA)} == {2}
    assert [r["seq"] for r in rec.of(Events.RESPONSE_READY)] == [2]
    assert rec.of(Events.RESPONSE_CANCELLED)[0]["seq"] == 1
    assert ctl.context.turn_count == 1 and provider.cancelled >= 1
    ctl.stop()


def test_cancel_discards_answer_and_context():
    gate = threading.Event()
    ctl, rec, *_ = make(ScriptedProvider(gate=gate), inline=False)
    ctl.submit("question", "typed", 1)
    assert ctl.busy and ctl.cancel("user")
    gate.set()
    assert wait_for(lambda: not ctl.busy)
    assert not rec.of(Events.RESPONSE_READY) and ctl.context.turn_count == 0
    ctl.stop()


# ---------------------------------------------------------------- errors and retries
def test_rate_limit_retried_with_backoff_then_succeeds():
    provider = ScriptedProvider(errors=[E.RateLimitError(retry_after=0.01), E.NetworkError()])
    ctl, rec, *_ = make(provider, max_retries=2)
    ctl.submit("hi", "typed", 1)
    assert rec.of(Events.RESPONSE_READY) and len(rec.of(Events.AI_RETRYING)) == 2
    assert rec.of(Events.AI_RETRYING)[0]["wait_s"] == 0.2  # minimum backoff


@pytest.mark.parametrize("error", [E.AuthError(), E.QuotaError(), E.ModelNotFoundError(), E.BadRequestError()])
def test_non_recoverable_errors_are_never_retried(error):
    provider = ScriptedProvider(errors=[error])
    ctl, rec, *_ = make(provider, max_retries=3)
    ctl.submit("hi", "typed", 1)
    assert len(provider.requests) == 1 and not rec.of(Events.AI_RETRYING)
    failed = rec.of(Events.RESPONSE_FAILED)[0]
    assert failed["kind"] == error.kind and failed["seq"] == 1


def test_long_retry_hint_is_left_to_the_user():
    provider = ScriptedProvider(errors=[E.RateLimitError(retry_after=45)])
    ctl, rec, *_ = make(provider)
    ctl.submit("hi", "typed", 1)
    assert len(provider.requests) == 1 and rec.of(Events.RESPONSE_FAILED)


def test_retries_are_bounded():
    provider = ScriptedProvider(errors=[E.TimeoutError_(retry_after=0.01)] * 10)
    ctl, rec, *_ = make(provider, max_retries=2)
    ctl.submit("hi", "typed", 1)
    assert len(provider.requests) == 3 and rec.of(Events.RESPONSE_FAILED)


def test_mid_stream_failure_keeps_partial_and_is_not_retried():
    provider = ScriptedProvider(pieces=["Part one. ", "Part two. ", "Part three."],
                                errors=[E.NetworkError()], fail_after=2)
    ctl, rec, *_ = make(provider, max_retries=2)
    ctl.submit("hi", "typed", 1)
    failed = rec.of(Events.RESPONSE_FAILED)[0]
    assert failed["partial"] == "Part one. Part two. " and len(provider.requests) == 1
    assert ctl.context.turn_count == 0


def test_empty_response_is_reported():
    ctl, rec, *_ = make(ScriptedProvider(pieces=["   "]))
    ctl.submit("hi", "typed", 1)
    assert rec.of(Events.RESPONSE_FAILED)[0]["kind"] == "empty"


def test_retry_after_failure_and_recovery():
    provider = ScriptedProvider(errors=[E.QuotaError("quota used up")])
    ctl, rec, *_ = make(provider)
    ctl.submit("question", "voice", 1)
    assert ctl.can_retry and rec.of(Events.RESPONSE_FAILED)[0]["error"] == "quota used up"
    assert ctl.retry(2)
    assert rec.of(Events.RESPONSE_READY)[0]["seq"] == 2 and not ctl.can_retry
    assert rec.of(Events.AI_STARTED)[-1]["source"] == "voice"


def test_missing_key_reports_config_error_without_crashing():
    bus = EventBus(synchronous=True)
    rec = RecordingBus(bus)
    ctl = ConversationController(bus, AIConfig(provider="gemini", api_key=None), ROOT, run_inline=True)
    ctl.start()
    assert rec.of(Events.AI_STATUS)[0]["ready"] is False
    ctl.submit("hello", "typed", 1)
    failed = rec.of(Events.RESPONSE_FAILED)[0]
    assert failed["kind"] == "config" and "GEMINI_API_KEY" in failed["error"]


def test_unexpected_bug_is_reported_without_traceback():
    class Buggy(ScriptedProvider):
        def stream_response(self, request, cancel=None):
            raise ZeroDivisionError("oops")

    ctl, rec, *_ = make(Buggy())
    ctl.submit("hi", "typed", 1)
    failed = rec.of(Events.RESPONSE_FAILED)[0]
    assert "Traceback" not in failed["error"] and "ZeroDivision" not in failed["error"]


# ---------------------------------------------------------------- switching
def test_provider_switching_keeps_context_and_rebuilds_provider():
    ctl, rec, provider, _clock, built = make()
    ctl.submit("first", "typed", 1)
    ctl.reconfigure(AIConfig(provider="echo", openai_model="gpt-other", context_turns=3))
    ctl.submit("second", "typed", 2)
    assert len(built) == 2 and built[1].context_turns == 3
    assert [m.content for m in provider.requests[-1].messages][:2] == ["first", "Answer to: first."]
    assert rec.of(Events.AI_STATUS)


def test_non_streaming_mode_uses_generate_response():
    ctl, rec, *_ = make(streaming=False)
    ctl.submit("hi", "typed", 1)
    assert not rec.of(Events.RESPONSE_DELTA) and rec.of(Events.RESPONSE_READY)[0]["text"] == "Answer to: hi."


def test_health_check_does_not_need_a_submission():
    ctl, *_ = make()
    assert ctl.health_check().ok


# ---------------------------------------------------------------- speech formatting
def test_speech_cleanup():
    md = ("## Title\n**Bold** and *italic* with `code` and a [link](https://x.y/z) or https://www.example.com/page.\n"
          "- item one\n- item two\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n```python\nline1\nline2\nline3\n```\n"
          "snake_case & 2 × 3 = 6 😀")
    spoken = to_speech(md)
    for bad in ("**", "`", "##", "https", "|", "😀", "```"):
        assert bad not in spoken
    assert "Bold and italic with code and a link or example.com." in spoken
    assert "item one. item two." in spoken and "A, B. 1, 2." in spoken
    assert CODE_NOTE in spoken and "snake case and 2 times 3 equals 6" in spoken
    assert to_speech("Traceback (most recent call last):\n File x") == ""


@pytest.mark.parametrize("step", [1, 2, 5, 13, 1000])
def test_chunker_is_independent_of_stream_chunk_sizes(step):
    text = "First sentence here. Second one!\n```js\nlet a = 1;\nlet b = 2;\nlet c = 3;\n```\nLast `bit` done."
    chunker, out = SpeechChunker(), []
    for i in range(0, len(text), step):
        out += chunker.push(text[i:i + step])
    out += chunker.flush()
    assert out == ["First sentence here. Second one!", CODE_NOTE, "Last bit done."]


@pytest.mark.parametrize("text, spoken", [
    (r"I calculated $15 \times 8$ by splitting it: $10 \times 8 = 80$.",
     "I calculated 15 times 8 by splitting it: 10 times 8 equals 80."),
    (r"Area is $\pi r^2$ and half is $\frac{1}{2}$ of $x^3$.",
     "Area is pi r squared and half is 1 over 2 of x to the power of 3."),
    (r"Use \(a \cdot b\) here.", "Use a times b here."),
    ("It costs $5 and $10 in total.", "It costs $5 and $10 in total."),  # money is not maths
])
def test_latex_maths_is_spoken_as_words(text, spoken):
    assert to_speech(text) == spoken


def test_system_prompt_forbids_latex():
    assert "Never use LaTeX" in load_base_prompt(ROOT / "config" / "system_prompt.txt")
