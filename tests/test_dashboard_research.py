"""RESEARCH page: the controls reach the real research services; results and citations display correctly."""

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from tests.research_env import ResearchEnv  # noqa: E402
from tests.research_fakes import article  # noqa: E402
from tests.test_dashboard_accounts import pump, until  # noqa: E402

DOC = "https://docs.example.org/guide"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def ui(qapp, paths):
    from dashboard.dashboard_controller import DashboardController
    from dashboard.main_window import MainWindow

    env = ResearchEnv(paths, search={"answer": "The guide explains the topic [1].", "points": [
        {"text": "It has examples [1].", "sources": [1], "kind": "fact"}], "uncertainty": "", "insufficient": False})
    env.web_results(("The Guide", DOC, "guide snippet", None, "unknown"))
    env.web.add(DOC, article("The Guide", "A detailed explanation with examples. " * 30))
    env.web.add("https://docs.example.org/robots.txt", "", status=404, ctype="text/plain")
    c = DashboardController(env.core)
    w = MainWindow(c)
    c.wake("test")
    w.show()
    until(lambda: w.isVisible())
    w._enter_shell()
    yield env, c, w
    w._quitting = True
    w.close()
    c.shutdown()


def test_research_page_runs_and_shows_answer_with_sources(ui):
    env, c, w = ui
    w.top.group.button(w.PAGES.index("RESEARCH")).click()
    pump(50)
    page = w.research
    page.query.setText("the guide topic")
    page.mode.setCurrentIndex(1)                                  # Web search
    page.run()
    assert until(lambda: page._record is not None, 10)
    assert "The guide explains the topic" in page.answer.toPlainText()
    assert DOC in page.answer.toMarkdown()                        # citation links to the retrieved source
    assert page.sources.rowCount() == 1 and page.sources.item(0, 4).text() == "page read"
    assert "✓ Search the web" in page.stage_line.text() and "✓ Check citations" in page.stage_line.text()
    assert not page.cancel_button.isEnabled()
    page.sources.selectRow(0)
    pump(50)
    assert "docs.example.org" in page.source_detail.toPlainText()
    # the same request appeared in the chat (same conversation path as typing and voice)
    assert until(lambda: any("The guide explains" in a.text for a in w.assistant.conversation.answers()), 5)
    assert w.assistant.rows["research"].value.text().startswith("completed")


def test_history_and_saved_findings_tabs(ui):
    env, c, w = ui
    env.say("search for the guide topic")
    w.top.group.button(w.PAGES.index("RESEARCH")).click()
    page = w.research
    page.tabs.setCurrentIndex(1)
    assert until(lambda: page.history_table.rowCount() == 1, 5)
    page.history_table.selectRow(0)
    page._save_history()
    assert until(lambda: any("research:" in m["memory_key"] for m in env.core.memory.list(env.session)), 5)
    page.tabs.setCurrentIndex(2)
    assert until(lambda: page.saved_table.rowCount() == 1, 5)
    page.saved_table.selectRow(0)
    page._delete_saved()
    assert until(lambda: not any("research:" in m["memory_key"] for m in env.core.memory.list(env.session)), 5)
    page.tabs.setCurrentIndex(1)
    assert until(lambda: page.history_table.rowCount() == 1, 5)
    page.history_table.selectRow(0)
    page._delete_history()
    assert until(lambda: env.core.research_history.list(env.session) == [], 5)


def test_settings_tab_saves_through_the_settings_service(ui):
    env, c, w = ui
    page = w.research
    w.top.group.button(w.PAGES.index("RESEARCH")).click()
    page.tabs.setCurrentIndex(3)
    assert until(lambda: page.region.text() == "IN", 5)
    assert "Tavily: not set" in page.keys.text()
    page.max_pages.setValue(5)
    page._save_settings()
    assert until(lambda: env.core.settings.get("research", "max_pages") == 5, 5)


def test_italic_notes_render_as_italics_not_link_like_underlines(qapp):
    # Regression: Qt's Markdown reader turned _note_ into an underline, so notes looked like links.
    from dashboard.widgets.conversation_panel import markdown_html

    html = markdown_html("_Saved on the SCHEDULE page._ See my_file.py, `snake_case` and https://x.com/a_b")
    assert "font-style:italic" in html and "text-decoration: underline;\">Saved" not in html
    assert "my_file.py" in html and "snake_case" in html and "https://x.com/a_b" in html
