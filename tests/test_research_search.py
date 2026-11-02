"""Module 5 feature 1: live web search, providers, failures, de-duplication, citations."""

import json

import httpx
import pytest

from research.http import default_client
from research.models import SearchResult
from research.providers.brave import BraveProvider
from research.providers.gdelt import GdeltProvider
from research.providers.google_news import GoogleNewsProvider
from research.providers.tavily import TavilyProvider
from research.search_provider import SearchError
from tests.research_env import ResearchEnv
from tests.research_fakes import ago, article

DOCS = "https://docs.python.org/3/whatsnew/3.11.html"
BLOG = "https://example-blog.com/python-311"


@pytest.fixture
def env(paths):
    e = ResearchEnv(paths, search={"answer": "Python 3.11 was released in October 2022 and is faster [1].",
                                   "points": [{"text": "It is 10-60% faster than 3.10 [1][2].", "sources": [1, 2],
                                               "kind": "fact"}],
                                   "uncertainty": "", "insufficient": False})
    yield e
    e.close()


def script_docs(env):
    env.web_results(("What's New In Python 3.11", DOCS, "Python 3.11 is between 10-60% faster", None, "unknown"),
                    ("Python 3.11 review", BLOG + "?utm_source=x", "A blog post", None, "unknown"),
                    ("Python 3.11 review (copy)", "https://www.example-blog.com/python-311/", "Same post", None,
                     "unknown"))
    env.web.add(DOCS, article("What's New In Python 3.11", "Python 3.11 was released on October 24, 2022.\n\n"
                              "CPython 3.11 is on average 1.25x faster than 3.10. " * 3))
    env.web.add(BLOG + "?utm_source=x", article("Python 3.11 review", "The release brings big speed-ups. " * 20))
    env.web.add("https://example-blog.com/robots.txt", "", status=404, ctype="text/plain")
    env.web.add("https://docs.python.org/robots.txt", "", status=404, ctype="text/plain")


def test_successful_search_cites_retrieved_sources(env):
    script_docs(env)
    reply = env.say("search for python 3.11 what's new")
    assert reply.kind == "research"
    assert f"[[1]]({DOCS})" in reply.text                       # citations link to the real retrieved page
    assert "**Sources**" in reply.text and "docs.python.org" in reply.text and "page read" in reply.text
    assert "searched with its own browser" in reply.text        # keyless fallback is disclosed
    assert "http" not in reply.speech and "[" not in reply.speech  # spoken summary has no links or markers
    row = env.history()[0]
    assert row["status"] == "completed" and row["kind"] == "search"
    saved = env.core.research_history.get(env.session, row["id"])
    assert {s["url"] for s in saved["sources"]} >= {DOCS}
    assert all(len(s["extract"]) <= 500 for s in saved["sources"])  # no whole articles stored


def test_duplicate_results_become_one_source(env):
    script_docs(env)
    env.say("search for python 3.11 what's new")
    sources = env.core.research_assistant.last(env.session).result.sources
    assert len([s for s in sources if "example-blog.com" in s.url]) == 1


def test_empty_results_are_reported_without_an_ai_answer(env):
    env.web_results()
    reply = env.say("search for zzqqxx nonexistent thing")
    assert "found no results" in reply.text
    assert env.ai.of("search") == []           # nothing was "answered" from model knowledge


def test_search_failure_is_reported_honestly(env):
    env.providers["browser"].errors = [SearchError("timeout", "browser"), SearchError("timeout", "browser")]
    reply = env.say("search for python release schedule")
    assert "Live web search failed" in reply.text and "timed out" in reply.text
    assert "won't answer from older knowledge" in reply.text
    assert len(env.providers["browser"].calls) == 2   # one careful retry, then a clear failure
    assert env.history()[0]["status"] == "failed"


def test_missing_credentials_never_switch_providers_silently(env):
    env.core.settings.update({"research": {"web_provider": "tavily"}})
    reply = env.say("search for python release schedule")
    assert "TAVILY_API_KEY" in reply.text and "Live web search failed" in reply.text
    assert env.providers["browser"].calls == [] and env.providers["tavily"].calls == []


def test_search_switched_off(env):
    env.core.settings.update({"research": {"web_provider": "off"}})
    reply = env.say("search for python release schedule")
    assert "switched off" in reply.text


def test_ordinary_questions_are_not_researched(env):
    for text in ("tell me a joke", "explain recursion", "what is the capital of France?", "check my laptop performance"):
        reply = env.say(text)
        assert reply is None or reply.kind != "research", text
    assert all(not p.calls for p in env.providers.values())


def test_time_sensitive_questions_are_researched_automatically(env):
    script_docs(env)
    reply = env.say("what is the latest version of python?")
    assert reply.kind == "research"
    env.core.settings.update({"research": {"auto_research": False}})
    assert env.say("what is the latest version of python?") is None


def test_inaccessible_page_is_marked_snippet_only(env):
    env.web_results(("Paywalled article", "https://news.example.com/a", "Snippet text about prices", None, "unknown"))
    env.web.add("https://news.example.com/a", "denied", status=403)
    env.web.add("https://news.example.com/robots.txt", "", status=404, ctype="text/plain")
    env.say("search for prices of things")
    s = env.core.research_assistant.last(env.session).result.sources[0]
    assert not s.read and "refused access" in s.read_error and "not bypassed" in s.read_error


def test_robots_txt_is_respected(env):
    env.web_results(("Some page", "https://strict.example.com/page", "snippet", None, "unknown"))
    env.web.add("https://strict.example.com/robots.txt", "User-agent: *\nDisallow: /", ctype="text/plain")
    env.web.add("https://strict.example.com/page", article("Page", "text " * 100))
    env.say("search for strict page")
    s = env.core.research_assistant.last(env.session).result.sources[0]
    assert not s.read and "robots.txt" in s.read_error
    assert "https://strict.example.com/page" not in env.web.requests


def test_local_and_private_addresses_are_never_fetched(env):
    env.web_results(("Router admin", "http://192.168.1.1/admin", "x", None, "unknown"),
                    ("Local service", "http://localhost:8080/secrets", "x", None, "unknown"))
    env.say("search for router settings page")
    for s in env.core.research_assistant.last(env.session).result.sources:
        assert not s.read and "not a public internet site" in s.read_error
    assert not any("192.168" in u or "localhost" in u for u in env.web.requests)


# ------------------------------------------------------------------ provider adapters (mocked HTTP)
def mock(handler):
    def factory(timeout=15.0, **kw):
        kw.pop("follow_redirects", None)
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout, **kw)
    return factory


def test_tavily_parses_results_and_sends_key_in_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": [{"title": "T", "url": "https://a.example/x", "content": "C",
                                                      "published_date": "2026-10-20T10:00:00Z"}]})

    results = TavilyProvider("tvly-secret", mock(handler)).search("ai news", kind="news", days=3)
    assert results[0].url == "https://a.example/x" and results[0].published.day == 20
    assert seen["auth"] == "Bearer tvly-secret" and "tvly-secret" not in seen["url"]
    assert seen["body"]["topic"] == "news" and seen["body"]["days"] == 3


@pytest.mark.parametrize("status, kind", [(401, "auth"), (429, "rate_limit"), (432, "quota"), (503, "unavailable")])
def test_provider_http_errors_map_to_clear_kinds(status, kind):
    provider = TavilyProvider("k", mock(lambda r: httpx.Response(status, headers={"retry-after": "2"})))
    with pytest.raises(SearchError) as info:
        provider.search("x")
    assert info.value.kind == kind
    if kind == "rate_limit":
        assert info.value.retry_after == 2


def test_provider_timeout_and_network_errors():
    def timeout(_r):
        raise httpx.ConnectTimeout("slow")

    def offline(_r):
        raise httpx.ConnectError("no route")

    with pytest.raises(SearchError) as a:
        BraveProvider("k", mock(timeout)).search("x")
    with pytest.raises(SearchError) as b:
        BraveProvider("k", mock(offline)).search("x")
    assert a.value.kind == "timeout" and b.value.kind == "network"
    with pytest.raises(SearchError) as c:
        BraveProvider("", mock(offline)).search("x")
    assert c.value.kind == "config"


def test_brave_parses_web_and_news():
    def handler(request):
        assert request.headers["x-subscription-token"] == "bk"
        if "/news/" in request.url.path:
            assert request.url.params["freshness"] == "pw"
            return httpx.Response(200, json={"results": [{"title": "N", "url": "https://n.example/1", "age": "2 days ago",
                                                          "meta_url": {"hostname": "n.example"}}]})
        return httpx.Response(200, json={"web": {"results": [{"title": "W", "url": "https://w.example/",
                                                              "description": "d", "page_age": "2026-10-01T00:00:00"}]}})

    p = BraveProvider("bk", mock(handler))
    assert p.search("x")[0].published.month == 10
    news = p.search("x", kind="news", days=7)[0]
    assert news.published is not None and news.source_name == "n.example"


def test_google_news_feed_parsing():
    feed = """<?xml version="1.0"?><rss><channel><item><title>Big AI launch - Reuters</title>
    <link>https://news.google.com/rss/articles/abc</link><pubDate>Wed, 21 Oct 2026 12:00:00 GMT</pubDate>
    <source url="https://www.reuters.com">Reuters</source><description>&lt;a&gt;x&lt;/a&gt;</description></item>
    </channel></rss>"""
    p = GoogleNewsProvider(mock(lambda r: httpx.Response(200, content=feed.encode())))
    r = p.search("ai", days=7)[0]
    assert r.title == "Big AI launch" and r.source_name == "Reuters" and r.site_url == "https://www.reuters.com"
    assert r.published.isoformat().startswith("2026-10-21T12:00")


def test_gdelt_parsing_and_text_errors():
    ok = {"articles": [{"url": "https://g.example/a", "title": "A sufficiently long headline here",
                        "seendate": "20261021T184500Z", "domain": "g.example"}]}
    r = GdeltProvider(mock(lambda req: httpx.Response(200, json=ok))).search("ai chips", days=3)[0]
    assert r.date_basis == "news index" and r.published.hour == 18
    with pytest.raises(SearchError):
        GdeltProvider(mock(lambda req: httpx.Response(200, text="Your query was too short"))).search("ai chips")


def test_default_client_identifies_dumboo():
    with default_client() as client:
        assert "DUMBOO-Research" in client.headers["user-agent"]


def test_a_slow_page_cannot_hold_up_the_answer(env, monkeypatch):
    import time

    import research.research_controller as rc

    script_docs(env)
    monkeypatch.setitem(rc.READ_BUDGET_S, "search", 1.0)
    real = env.web.handler

    def slow(request):
        if "example-blog.com/python-311" in str(request.url):
            time.sleep(3.0)                                   # this page takes far longer than the budget
        return real(request)
    env.web.handler = slow
    started = time.monotonic()
    reply = env.say("search for python 3.11 what's new")
    assert time.monotonic() - started < 2.8                     # answered without waiting for the slow page
    assert f"[[1]]({DOCS})" in reply.text                     # the page that was read is still cited
    assert "took longer than 1 s" in reply.text or "snippet only" in reply.text
