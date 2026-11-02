"""Module 5 integration: voice and text share one path, cancellation, no blocking, persistence, follow-ups."""

import threading
import time

import pytest

from app.event_bus import Events
from research.models import SearchResult
from research.search_provider import SearchProvider
from tests.fakes import RecordingBus, wait_for
from tests.research_env import ResearchEnv
from tests.research_fakes import ago, article
from tests.test_application import OWNER, make_core

DOC = "https://docs.example.org/guide"


@pytest.fixture
def env(paths):
    e = ResearchEnv(paths, search={"answer": "The guide explains it [1].", "points": [], "uncertainty": "",
                                   "insufficient": False},
                    news={"overview": "Two things happened [1].", "developments": [
                        {"headline": "First thing", "summary": "One [1].", "sources": [1], "kind": "fact"},
                        {"headline": "Second thing", "summary": "Two [2].", "sources": [2], "kind": "fact"}]})
    e.web_results(("Guide", DOC, "the guide", None, "unknown"))
    e.web.add(DOC, article("Guide", "Detailed explanation of the topic. " * 30))
    e.web.add("https://docs.example.org/robots.txt", "", status=404, ctype="text/plain")
    yield e
    e.close()


def test_voice_and_typed_requests_use_the_same_research_controller(env):
    rec = RecordingBus(env.core.bus)
    for source in ("typed", "voice"):
        env.core.bus.publish(Events.USER_TEXT, {"text": f"search for the guide {source}", "source": source})
        assert wait_for(lambda: len([p for p in rec.of(Events.RESPONSE_READY) if p.get("model") == "research"]) >= (
            1 if source == "typed" else 2), 10)
    rows = env.history()
    assert sorted(r["source"] for r in rows) == ["typed", "voice"]
    voice = [p for p in rec.of(Events.RESPONSE_READY) if p.get("model") == "research"][-1]
    assert "http" not in voice["speech"] and "[" not in voice["speech"]   # spoken: short, no links


class GatedProvider(SearchProvider):
    name = "browser"
    kinds = ("web",)

    def __init__(self):
        self.gate = threading.Event()
        self.started = threading.Event()

    def search(self, query, **kw):
        self.started.set()
        self.gate.wait(10)
        return [SearchResult("Guide", DOC, "the guide")]


def test_research_does_not_block_and_can_be_cancelled(env):
    gated = GatedProvider()
    env.core.research.search_service._providers["browser"] = gated
    rec = RecordingBus(env.core.bus)
    env.core.bus.publish(Events.USER_TEXT, {"text": "search for slow topic", "source": "typed"})
    assert gated.started.wait(5)
    # while the search waits, the event bus (and so the dashboard) keeps working
    t0 = time.monotonic()
    env.core.bus.publish(Events.MIC_STATUS, {"open": True, "device": "x", "error": None})
    env.core.bus.drain()
    assert time.monotonic() - t0 < 1.0
    assert rec.of(Events.RESEARCH_PROGRESS) and rec.of(Events.RESEARCH_PROGRESS)[-1]["task"]["status"] == "running"
    env.core.bus.publish(Events.STOP_SPEAKING, {"source": "ui"})     # the STOP button
    gated.gate.set()
    assert wait_for(lambda: any(p["status"] == "cancelled" for p in rec.of(Events.RESEARCH_DONE)), 10)
    assert wait_for(lambda: env.history() and env.history()[0]["status"] == "cancelled", 5)


def test_progress_reflects_real_stages(env):
    rec = RecordingBus(env.core.bus)
    env.say("search for the guide")
    env.core.bus.drain()
    snaps = [p["task"] for p in rec.of(Events.RESEARCH_PROGRESS)]
    seen = [next((s["description"] for s in snap["steps"] if s["status"] == "running"), None) for snap in snaps]
    assert "Search the web" in seen and "Read sources" in seen and "Analyse evidence" in seen
    assert snaps[-1]["status"] == "completed" and all(s["status"] in ("completed", "skipped")
                                                      for s in snaps[-1]["steps"])


def test_failures_are_shown_accurately(env):
    from research.search_provider import SearchError

    env.providers["browser"].errors = [SearchError("network", "browser")] * 2
    rec = RecordingBus(env.core.bus)
    env.core.bus.publish(Events.USER_TEXT, {"text": "search for anything", "source": "typed"})
    assert wait_for(lambda: rec.of(Events.RESPONSE_READY), 10)
    assert "Live web search failed" in rec.of(Events.RESPONSE_READY)[0]["text"]
    assert "no connection" in rec.of(Events.RESPONSE_READY)[0]["text"]


def test_research_history_persists_across_restarts(paths, env):
    env.say("search for the guide")
    rid = env.history()[0]["id"]
    env.close()
    core = make_core(paths, signed_in=False)
    try:
        core.start()
        core.accounts.login(OWNER[0], OWNER[2])
        row = core.research_history.get(core.accounts.session, rid)
        assert row["status"] == "completed" and row["sources"][0]["url"] == DOC and "[[1]]" in row["answer"]
    finally:
        core.shutdown()
    env.close = lambda: None


def test_follow_up_uses_the_previous_answer(env):
    env.news_results(SearchResult("First thing happened", "https://a.example/1", "", ago(hours=2), "publisher"),
                     SearchResult("Second thing happened elsewhere", "https://b.example/2", "", ago(hours=3), "publisher"))
    env.say("what are the latest AI developments?")
    env.providers["browser"].calls.clear()
    reply = env.say("Explain the second development in more detail.")
    assert reply.kind == "research"
    assert env.providers["browser"].calls[0][0].startswith("Second thing")     # searched for that development
    last = env.core.research_assistant.last(env.session).result
    assert any(s.url == "https://b.example/2" for s in last.sources)           # its earlier source was kept


def test_show_sources_without_a_new_search(env):
    env.say("search for the guide")
    env.providers["browser"].calls.clear()
    reply = env.say("show me the sources")
    assert DOC in reply.text and env.providers["browser"].calls == []


def test_export_report_goes_through_automation(env):
    env.say("search for the guide")
    reply = env.say("export this research report")
    assert "Saved the report" in reply.text
    task = env.core.automation_history.list(env.session)[0]
    assert task["steps"][0]["capability"] == "files.create_file" and task["status"] == "completed"


def test_existing_modules_are_unchanged(env):
    assert env.say("remember that my favourite colour is blue").kind == "memory"
    assert env.say("check my laptop performance").kind == "automation"
    assert env.say("open youtube").kind == "automation"
    assert env.say("tell me a joke") is None   # goes to the AI conversation as before
