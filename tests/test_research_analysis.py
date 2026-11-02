"""Module 5 features 3 and 4: multi-step research, source comparison, fact-checking, citation validation."""

import pytest

from ai import errors as E
from research.citation_service import CitationService
from research.models import Finding, ResearchResult, Source
from research.quality import assign_groups, independent_count
from research.research_planner import fallback_plan
from tests.research_env import ResearchEnv
from tests.research_fakes import ago, article

A = "https://official.example.gov/models"
B = "https://www.reuters.com/models-compared"
C = "https://blog.example.com/models"

PLAN = {"sub_questions": [{"question": "Which AI models lead benchmarks?", "queries": ["AI model benchmark leaders"]},
                          {"question": "What do they cost?", "queries": ["AI model pricing official"]}],
        "needs_news": False}
REPORT = {"executive_summary": "Model X leads most benchmarks [1], but pricing differs [2].",
          "findings": [{"text": "Model X scores highest on the official benchmark [1].", "sources": [1], "kind": "fact"},
                       {"text": "Reuters reports Model Y is cheaper [2].", "sources": [2], "kind": "claim"},
                       {"text": "Model Z is secretly the best.", "sources": [9], "kind": "fact"},
                       {"text": "See https://made-up.example/proof for details [1].", "sources": [1], "kind": "fact"}],
          "comparison": "The official page [1] and Reuters [2] agree on rankings; the blog [3] differs.",
          "uncertainty": "Benchmarks change often.", "unanswered": ["Long-term reliability"]}


@pytest.fixture
def env(paths):
    e = ResearchEnv(paths, plan=PLAN, report=REPORT)
    yield e
    e.close()


def script_sources(env, provider="browser"):
    env.providers[provider].results["web"] = [
        __import__("research.models", fromlist=["SearchResult"]).SearchResult(t, u, s, None, "unknown")
        for t, u, s in (("Official model benchmarks", A, "benchmarks"), ("Models compared", B, "pricing"),
                        ("My blog on models", C, "opinion"))]
    env.web.add(A, article("Official model benchmarks", "Model X has the highest score on the benchmark. " * 10))
    env.web.add(B, article("Models compared", "Model Y costs less than Model X, Reuters found. " * 10))
    env.web.add(C, article("My blog", "I think Model Z is best. " * 10))
    for host in ("official.example.gov", "www.reuters.com", "blog.example.com"):
        env.web.add(f"https://{host}/robots.txt", "", status=404, ctype="text/plain")


def test_multi_step_research_plan_searches_and_report(env):
    script_sources(env)
    reply = env.say("Research the best AI models and their pricing and give me a detailed report with references.")
    calls = [c[0] for c in env.providers["browser"].calls]
    assert "AI model benchmark leaders" in calls and "AI model pricing official" in calls   # one search per step
    text = reply.text
    for section in ("**Executive summary**", "**Main findings**", "**Source comparison**",
                    "**Uncertainty and limitations**", "**Sources**"):
        assert section in text
    assert f"[[1]]({A})" in text and f"[[2]]({B})" in text      # claims linked to the right sources
    assert "secretly the best" not in text                        # cited a source that doesn't exist -> dropped
    assert "made-up.example" not in text and "link removed" in text
    assert "_(reported claim)_" in text and "Unanswered: Long-term reliability" in text
    row = env.core.research_history.get(env.session, env.history()[0]["id"])
    assert [p["question"] for p in row["plan"]] == [q["question"] for q in PLAN["sub_questions"]]


def test_plan_without_ai_splits_comparisons():
    plan = fallback_plan("Compare Gemini, ChatGPT and Claude using current information")
    assert [sq.queries[0] for sq in plan.sub_questions][:3] == ["Gemini", "ChatGPT", "Claude"]
    assert len(fallback_plan("history of the printing press").sub_questions) == 1


def test_ai_unavailable_shows_sources_honestly(env):
    script_sources(env)
    env.ai.errors = [E.QuotaError(provider="gemini")] * 10
    reply = env.say("search for best AI models pricing")
    assert "couldn't write a summary" in reply.text and "what the sources themselves say" in reply.text
    assert A in reply.text and "no AI summary" in reply.text
    assert env.history()[0]["status"] == "partial"


def test_citation_service_rejects_unknown_ids_and_urls():
    result = ResearchResult("search", "q", sources=[Source(1, "A", A), Source(2, "B", B)])
    cite = CitationService(result)
    assert cite.clean_text("Fact [1][7] see https://evil.example/x and " + A) == \
        f"Fact [1] see (link removed: not a retrieved source) and {A}"
    findings = cite.finalize([Finding("supported [2]", [2]), Finding("no source", [5]), Finding("my view", [],
                                                                                                    "analysis")])
    assert [f.text for f in findings] == ["supported [2]", "my view"]
    assert cite.report.removed_ids >= 2 and cite.report.unsupported == 1


def test_syndicated_copies_are_not_independent():
    sources = [Source(1, "Company X recalls 1 million cars over brake fault", "https://a.example/1"),
               Source(2, "Company X recalls 1 million cars over brake fault - report", "https://b.example/2"),
               Source(3, "Regulator confirms recall investigation", "https://c.example/3")]
    assert assign_groups(sources) == 2 and independent_count(sources[:2]) == 1


FACTS = {"claims": [{"claim": "Python 3.11 was released in October 2022", "verdict": "supported",
                     "explanation": "The official release notes give 24 October 2022 [1].", "supporting": [1],
                     "contradicting": [], "note": ""},
                    {"claim": "Python 3.11 is 5x faster", "verdict": "contradicted",
                     "explanation": "The docs say 1.25x on average [1].", "supporting": [], "contradicting": [1],
                     "note": "Some benchmarks are faster"},
                    {"claim": "Python 3.11 was written by aliens", "verdict": "supported",
                     "explanation": "Everyone says so.", "supporting": [], "contradicting": [], "note": ""}]}


def test_fact_check_verdicts_and_evidence(paths):
    env = ResearchEnv(paths, claims={"claims": ["Python 3.11 was released in October 2022",
                                                "Python 3.11 is 5x faster", "Python 3.11 was written by aliens"]},
                      verdict=FACTS)
    try:
        docs = "https://docs.python.org/3/whatsnew/3.11.html"
        env.providers["browser"].results["web"] = [
            __import__("research.models", fromlist=["SearchResult"]).SearchResult("What's new", docs, "s", None)]
        env.web.add(docs, article("What's New", "Python 3.11 was released on October 24, 2022. It is 1.25x faster. "
                                  * 5))
        env.web.add("https://docs.python.org/robots.txt", "", status=404, ctype="text/plain")
        claim = ("Python 3.11 was released in October 2022. It is 5x faster than Python 3.10 in every program. "
                 "It was written by aliens who visited the core developers in a dream last year.")
        reply = env.say(f"verify: {claim}")
        text = reply.text
        assert "**✅ Supported** — “Python 3.11 was released in October 2022”" in text
        assert "**❌ Contradicted** — “Python 3.11 is 5x faster”" in text and "Contradicting:" in text
        assert "**❔ Not enough evidence** — “Python 3.11 was written by aliens”" in text  # "supported" w/o evidence
        assert "1 independent source" in text
        assert env.ai.of("claims") and env.ai.of("verdict")
    finally:
        env.close()


def test_verify_previous_answer_uses_the_conversation(paths):
    env = ResearchEnv(paths, verdict={"claims": [{"claim": "x", "verdict": "insufficient", "explanation": "No source "
                                                  "settles it.", "supporting": [], "contradicting": []}]})
    try:
        env.core.conversation.context.add_turn("When was Python 3.11 released?",
                                               "Python 3.11 was released in October 2022.", "typed")
        env.providers["browser"].results["web"] = [
            __import__("research.models", fromlist=["SearchResult"]).SearchResult("Doc", "https://d.example/x", "s")]
        reply = env.say("Verify whether this information is correct.")
        assert "Not enough evidence" in reply.text
        assert env.providers["browser"].calls[0][0].startswith("Python 3.11 was released in October 2022")
    finally:
        env.close()


def test_unresolvable_referent_asks_instead_of_guessing(env):
    reply = env.say("Search for the official requirements for this course.")
    assert reply.text.startswith("Which course do you mean?") and reply.listen_after
    assert all(not p.calls for p in env.providers.values())


def test_one_organisation_is_one_origin_and_github_labels():
    from research.quality import quality_label

    sources = [Source(1, "Python 3.11.0 release", "https://www.python.org/downloads/release/python-3110/"),
               Source(2, "What's new in 3.11", "https://docs.python.org/3.11/whatsnew/3.11.html")]
    assert assign_groups(sources) == 1                 # python.org and docs.python.org: same organisation
    assert quality_label("https://github.com/pywinauto/pywinauto", "pywinauto automation") == "official/primary"
    assert quality_label("https://github.com/someone/my-scripts", "python desktop automation") == "community"


def test_speech_and_unanswered_formatting(paths):
    from research.result_formatter import _plain

    assert _plain("Fast [[1]](https://x.example). Also [2], slow.") == "Fast. Also, slow."
    env = ResearchEnv(paths, plan=PLAN, report={**REPORT, "unanswered": [{"q": "Long-term costs?"}, "Support?"]})
    try:
        script_sources(env)
        reply = env.say("research the best AI models and their pricing")
        assert "Unanswered: Long-term costs?" in reply.text and "{'q'" not in reply.text
    finally:
        env.close()
