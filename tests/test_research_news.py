"""Module 5 feature 2: latest news — dates checked, old stories excluded, sources compared."""

from datetime import datetime, timedelta, timezone

import pytest

from research.models import SearchResult
from research.news_service import NewsService, window_days
from tests.research_env import ResearchEnv
from tests.research_fakes import ago, article

GN1 = "https://news.google.com/rss/articles/one"
GN2 = "https://news.google.com/rss/articles/two"
REUTERS = "https://www.reuters.com/tech/ai-chip-launch"
VERGE = "https://www.theverge.com/ai-chip-launch"


def news_answer(request):
    return {"overview": "AI chip makers launched new products this week [1].",
            "developments": [{"headline": "New AI chip launched", "summary": "A new chip was launched [1][2].",
                              "sources": [1, 2], "event_date": "20 October 2026", "why_it_matters": "Faster AI [1].",
                              "kind": "fact", "uncertainty": "Prices are not confirmed [2]."},
                             {"headline": "Invented story", "summary": "Something with no source.", "sources": [],
                              "kind": "fact", "event_date": "3 March 2020"}],
            "disagreements": "Reuters reports a higher price than The Verge [1][2]."}


@pytest.fixture
def env(paths):
    e = ResearchEnv(paths, news=news_answer)
    yield e
    e.close()


def script_news(env):
    env.news_results(SearchResult("New AI chip launched", GN1, "", ago(hours=5), "publisher", "Reuters",
                                  site_url="https://www.reuters.com"),
                     SearchResult("New AI chip is launched today", GN2, "", ago(hours=8), "publisher", "The Verge",
                                  site_url="https://www.theverge.com"),
                     SearchResult("Old chip story", "https://news.google.com/rss/articles/old", "", ago(days=40),
                                  "publisher", "Old News"),
                     SearchResult("Undated chip rumour", "https://news.google.com/rss/articles/undated", "", None,
                                  "unknown", "Blog"))
    env.browser.redirects = {GN1: REUTERS, GN2: VERGE}
    env.web.add(REUTERS, article("New AI chip launched", "The company unveiled the chip on 20 October 2026 at its "
                                 "event in California. It costs $500, Reuters reports. " * 4))
    env.web.add(VERGE, article("New AI chip", "The chip was announced on 20 October 2026; The Verge says it costs "
                               "$450. " * 4))
    for host in ("www.reuters.com", "www.theverge.com"):
        env.web.add(f"https://{host}/robots.txt", "", status=404, ctype="text/plain")


def test_latest_news_with_dates_period_and_links(env):
    script_news(env)
    reply = env.say("What are the latest AI developments?")
    text = reply.text
    assert "**News ·" in text and "last 7 days" in text                 # the period searched is stated
    assert "**1. New AI chip launched**" in text and f"[[1]]({REUTERS})" in text   # resolved original article
    assert "_Published: " in text and "event: 20 October 2026" in text  # event date kept: it is in the article
    assert "Invented story" not in text                                 # no source -> not presented
    assert "3 March 2020" not in text
    assert "Old chip story" not in text and "older article(s) outside the period were left out" in text
    assert "had no publication date" in text
    assert "How sources differ" in text and "Uncertain / disputed" in text
    assert "Here are" in reply.speech or "AI chip" in reply.speech
    assert env.providers["google_news"].calls[0][2] == 7                # the provider was asked for 7 days


def test_publication_date_comes_from_metadata_not_the_ai(env):
    script_news(env)
    env.say("What are the latest AI developments?")
    r = env.core.research_assistant.last(env.session).result
    newest = max(s.published for s in r.sources if s.id in (1, 2))
    assert r.findings[0].published.startswith(newest.astimezone().strftime("%d %b %Y"))


def test_event_date_not_in_sources_is_dropped(env):
    script_news(env)
    env.ai.answers["news"] = lambda r: {**news_answer(r), "developments": [
        {**news_answer(r)["developments"][0], "event_date": "1 January 2026"}]}
    reply = env.say("What are the latest AI developments?")
    assert "1 January 2026" not in reply.text


def test_today_window_and_no_old_news_as_new(env):
    env.news_results(SearchResult("Last month's launch", "https://news.google.com/rss/articles/x", "", ago(days=20),
                                  "publisher", "Reuters"))
    reply = env.say("Give me today's important technology news.")
    assert "I found no news" in reply.text and "last 1 day" in reply.text
    assert env.ai.of("news") == []                                    # nothing invented to fill the gap


def test_news_failure_is_not_hidden(env):
    from research.search_provider import SearchError

    env.providers["google_news"].errors = [SearchError("network", "google_news")] * 2
    env.providers["gdelt"].errors = [SearchError("network", "gdelt")] * 2
    reply = env.say("What happened recently in cybersecurity?")
    assert "Live news search failed" in reply.text


def test_same_story_from_several_publishers_is_grouped():
    now = datetime.now(timezone.utc)
    stories = NewsService.group([SearchResult("Rocket launch delayed by weather", "https://a.example/1", "",
                                              now, "publisher", site_url="https://a.example"),
                                 SearchResult("Rocket launch delayed by bad weather", "https://www.reuters.com/x", "",
                                              now, "publisher", site_url="https://www.reuters.com"),
                                 SearchResult("New phone released", "https://c.example/2", "", now, "publisher")])
    assert len(stories) == 2 and len(stories[0].articles) == 2
    assert "reuters.com" in stories[0].lead.url                     # the established outlet leads the story


def test_window_days_parsing():
    assert window_days("today's news", 7) == 1
    assert window_days("news this week", 7) == 7
    assert window_days("what happened this month in space", 7) == 31
    assert window_days("news from the last 3 days", 7) == 3
    assert window_days("latest AI news", 7) == 7


def test_future_and_old_dates_are_filtered():
    from research.web_search_service import SearchOutcome

    class Fake:
        def search(self, *a, **k):
            now = datetime.now(timezone.utc)
            return SearchOutcome([SearchResult("ok", "https://x.example/1", "", now - timedelta(hours=1)),
                                  SearchResult("future", "https://x.example/2", "", now + timedelta(days=3)),
                                  SearchResult("old", "https://x.example/3", "", now - timedelta(days=30))], ["fake"])

    out = NewsService(Fake()).latest("x", 7)
    assert [s.lead.title for s in out.stories] == ["ok"] and out.dropped_old == 2


def test_only_declared_dates_are_trusted():
    from research.dates import declared_date, url_date

    assert declared_date('<meta property="article:published_time" content="2026-10-21T12:09:22Z">').day == 21
    assert declared_date('<script>{"datePublished": "2022-10-24T10:00:00Z"}</script>').year == 2022
    assert declared_date("<html><p>Copyright 2012 Python Software Foundation</p></html>") is None  # no guessing
    assert url_date("https://blog.python.org/2022/10/python-3110-is-now-available/").month == 10
    assert url_date("https://docs.python.org/3/whatsnew/3.11.html") is None


def test_documentation_without_a_declared_date_is_date_unknown(paths):
    env = ResearchEnv(paths, search={"answer": "x [1]", "points": [], "uncertainty": ""})
    try:
        url = "https://docs.example.org/manual"
        env.web_results(("Manual", url, "manual", None, "unknown"))
        env.web.add(url, "<html><head><title>Manual</title></head><body><article>" + "<p>Copyright 2012. Text "
                    "about the manual.</p>" * 20 + "</article></body></html>")
        env.web.add("https://docs.example.org/robots.txt", "", status=404, ctype="text/plain")
        reply = env.say("search for the manual docs")
        assert "date unknown" in reply.text and "2012" not in reply.text.split("**Sources**")[1]
    finally:
        env.close()
