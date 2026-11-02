"""A headless DUMBOO wired to fake search providers, a fake web and a routing fake AI."""

from __future__ import annotations

from research.models import SearchResult
from tests.automation_fakes import FakeBrowser
from tests.research_fakes import FakeSearchProvider, FakeWeb, RoutingAI, fake_resolver
from tests.test_application import make_core


class ResearchEnv:
    def __init__(self, paths, environ: dict | None = None, **ai_answers) -> None:
        self.web = FakeWeb()
        self.ai = RoutingAI(**ai_answers)
        self.providers = {"tavily": FakeSearchProvider("tavily", needs_key=True),
                          "brave": FakeSearchProvider("brave", needs_key=True),
                          "google_news": FakeSearchProvider("google_news", kinds=("news",)),
                          "gdelt": FakeSearchProvider("gdelt", kinds=("news",)),
                          "browser": FakeSearchProvider("browser")}
        self.browser = FakeBrowser()
        env = {"AI_PROVIDER": "gemini", "GEMINI_API_KEY": "test-key-not-real"}
        env.update(environ or {})
        self.core = make_core(paths, ai_provider_factory=lambda _c: self.ai, environ=env,
                              research_providers=self.providers, research_client=self.web.client_factory(),
                              research_resolver=fake_resolver, research_browser=self.browser)
        self.sandbox = paths.data_dir.parent / "sandbox"     # never the user's real folders
        self.sandbox.mkdir(exist_ok=True)
        self.core.automation_settings.set_permitted_directories([{"path": str(self.sandbox), "write": True}])
        self.core.start()
        self.session = self.core.accounts.session

    # -- scripting ----------------------------------------------------------------------------
    def web_results(self, *items: tuple, provider: str = "browser") -> None:
        self.providers[provider].results["web"] = [SearchResult(*i) for i in items]

    def news_results(self, *items: SearchResult, provider: str = "google_news") -> None:
        self.providers[provider].results["news"] = list(items)

    def say(self, text: str, source: str = "typed", session=None):
        return self.core.research_assistant.local_reply(session or self.session, text, source)

    def history(self, session=None):
        return self.core.research_history.list(session or self.session)

    def close(self) -> None:
        self.core.shutdown()
