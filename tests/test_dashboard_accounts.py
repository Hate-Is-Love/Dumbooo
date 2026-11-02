"""Dashboard integration for Module 3: every control reaches the real services and database."""

import json
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app.event_bus import Events  # noqa: E402
from tests.ai_fakes import ScriptedProvider  # noqa: E402
from tests.fakes import wait_for  # noqa: E402
from tests.test_application import OWNER, make_core  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def pump(ms=30):
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def until(predicate, timeout=5.0):
    return wait_for(lambda: (pump(15), predicate())[1], timeout)


class UI:
    def __init__(self, paths, signed_in=True, **kw):
        from dashboard.dashboard_controller import DashboardController
        from dashboard.main_window import MainWindow

        self.provider = ScriptedProvider()
        self.core = make_core(paths, signed_in=signed_in, ai_provider_factory=lambda _c: self.provider, **kw)
        self.c = DashboardController(self.core)
        self.w = MainWindow(self.c)
        self.core.start()
        self.c.wake("test")
        assert until(lambda: self.w.isVisible())

    def page(self, name):
        index = self.w.PAGES.index(name)
        self.w.top.group.button(index).click()
        pump(50)
        return self.w.pages[index]

    def chat(self, text):
        convo = self.w.assistant.conversation
        before = len(convo.answers())
        convo.input.setText(text)
        convo._submit()
        assert until(lambda: len(convo.answers()) > before and convo.answers()[-1].done)
        self.core.recorder.flush()
        return convo.answers()[-1].text

    def close(self):
        self.w._quitting = True
        self.w.close()
        self.c.shutdown()


@pytest.fixture
def yes(monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)


def test_first_run_owner_setup_from_the_dashboard(qapp, paths):
    ui = UI(paths, signed_in=False)
    try:
        login = ui.w.login
        assert ui.w.root_stack.currentIndex() == 0 and login.stack.currentIndex() == 1  # owner setup form
        assert login.scanner.mode == "setup"
        login.setup_user.setText("roshan")
        login.setup_name.setText("Roshan")
        login.setup_pass.setText("Str0ng-Passw0rd")
        login.setup_confirm.setText("Str0ng-Passw0rd")
        login._setup()
        assert login.scanner.mode == "scanning"                  # identity scan animation while hashing
        assert until(lambda: login.scanner.mode == "granted")
        assert until(lambda: ui.w.root_stack.currentIndex() == 1)
        assert "ROSHAN · OWNER" in ui.w.top.user.text()
        assert login.setup_pass.text() == ""                     # passwords never linger in the form
        assert ui.core.auth.require(ui.core.accounts.session).is_owner
    finally:
        ui.close()


def test_login_wrong_then_right_password_and_lock(qapp, paths):
    first = UI(paths)  # creates the owner account
    first.close()
    ui = UI(paths, signed_in=False)
    try:
        login = ui.w.login
        assert login.stack.currentIndex() == 0
        login.login_user.setText(OWNER[0])
        login.login_pass.setText("wrong-password-1")
        login._login()
        assert until(lambda: login.scanner.mode == "denied")
        assert "Invalid username or password" in login.message.text()
        assert ui.w.root_stack.currentIndex() == 0
        login.login_pass.setText(OWNER[2])
        login._login()
        assert until(lambda: ui.w.root_stack.currentIndex() == 1)
        ui.chat("hello there")
        ui.w.top.lock.click()                                    # sign out
        assert until(lambda: ui.w.root_stack.currentIndex() == 0)
        assert ui.w.assistant.conversation.message_count() == 0  # private chat removed from the screen
        assert ui.core.accounts.session is None and ui.core.conversation.context.turn_count == 0
    finally:
        ui.close()


def test_history_page_lists_opens_searches_exports_and_deletes(qapp, paths, tmp_path, yes):
    ui = UI(paths)
    try:
        ui.chat("I bought a red bicycle")
        history = ui.page("HISTORY")
        assert until(lambda: history.list.count() == 1 and "bicycle" in history.list.item(0).text())
        assert history.activity.operations >= 1                  # live DB animation ran
        history._item_clicked(history.list.item(0))
        assert until(lambda: history._current is not None)
        assert "red bicycle" in history.view.toPlainText()
        history.search.setText("bicycle")
        history.run_search()
        assert until(lambda: "MATCHING" in history.summary.text())
        out = tmp_path / "export.json"
        history._export(str(out))
        assert until(lambda: out.exists())
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["conversations"][0]["messages"][0]["content"] == "I bought a red bicycle"
        assert "password_hash" not in out.read_text(encoding="utf-8")
        history._delete()
        assert until(lambda: ui.core.history.list_conversations(ui.core.accounts.session) == [])
    finally:
        ui.close()


def test_history_continue_restores_context(qapp, paths):
    ui = UI(paths)
    try:
        ui.chat("Tell me about Python")
        ui.w.assistant.conversation.clear()                      # NEW CHAT
        pump(100)
        history = ui.page("HISTORY")
        assert until(lambda: history.list.count() >= 1)
        history.open_conversation(ui.core.history.list_conversations(ui.core.accounts.session)[0]["id"])
        assert until(lambda: history._current is not None)
        history._continue()
        assert ui.w.stack.currentIndex() == 0
        assert ui.core.conversation.context.turn_count == 1
        ui.chat("What are its advantages?")
        sent = [m.content for m in ui.provider.requests[-1].messages]
        assert sent[:2] == ["Tell me about Python", "Answer to: Tell me about Python."]
        assert len(ui.core.history.list_conversations(ui.core.accounts.session)) == 1  # same conversation
    finally:
        ui.close()


def test_memory_page_add_edit_delete_clear(qapp, paths, yes):
    ui = UI(paths)
    try:
        memory_page = ui.page("MEMORY")
        memory_page._save("favourite fruit", "mango", "preference", None)
        assert until(lambda: memory_page.table.rowCount() == 1)
        assert memory_page.table.item(0, 1).text() == "mango"
        memory_page.table.selectRow(0)
        mem_id = memory_page._selected()["id"]
        memory_page._save("favourite fruit", "papaya", "preference", mem_id)
        assert until(lambda: memory_page.table.rowCount() == 1 and memory_page.table.item(0, 1).text() == "papaya")
        memory_page.table.selectRow(0)
        memory_page._delete()
        assert until(lambda: memory_page.table.rowCount() == 0)
        memory_page._save("pin", "1234", "fact", None)             # secrets are refused with a message
        assert until(lambda: "secrets" in memory_page.status.text())
        ui.chat("remember my city is Pune")
        assert until(lambda: memory_page.table.rowCount() == 1)   # chat command updated the page
        memory_page._clear_all()
        assert until(lambda: ui.core.memory.list(ui.core.accounts.session) == [])
    finally:
        ui.close()


def test_retrieval_drives_the_core_animation(qapp, paths):
    ui = UI(paths)
    try:
        ui.chat("remember my project is called DUMBOO")
        assert until(lambda: ui.w.assistant.core._flash_text == "MEMORY WRITTEN")
        ui.chat("what is my project called?")
        assert until(lambda: ui.w.assistant.core._mem_found is not None)
        assert ui.w.assistant.core._mem_found[0] >= 1             # facts found -> particle burst + panel count
        assert "retrieved" in ui.w.assistant.rows["memory"].value.text()
    finally:
        ui.close()


def test_profile_page_preferences_and_voice_toggle(qapp, paths):
    ui = UI(paths)
    try:
        profile = ui.page("ACCOUNT")
        assert until(lambda: profile.username.text() == OWNER[0])
        profile.title.setText("Boss")
        profile.voice_output.setCurrentText("off")
        profile._save_prefs()
        assert until(lambda: ui.core.profile.get(ui.core.accounts.session)["title"] == "Boss")
        assert until(lambda: not ui.w.assistant.voice_button.isChecked())  # button follows the preference
        ui.w.assistant.voice_button.click()
        assert until(lambda: ui.core.profile.get(ui.core.accounts.session)["voice_output"] == "on")
    finally:
        ui.close()


def test_admin_page_manages_users_with_reauth(qapp, paths, yes, monkeypatch):
    import dashboard.widgets.admin_panel as admin_mod
    import dashboard.widgets.reauth as reauth_mod

    shown = []
    monkeypatch.setattr(admin_mod, "show_secret_once", lambda parent, title, text, secret: shown.append(secret))
    real_run = reauth_mod.run_sensitive
    monkeypatch.setattr(admin_mod, "run_sensitive",
                        lambda *a, **k: real_run(*a, ask_password=lambda w, t: OWNER[2], **k))
    ui = UI(paths)
    try:
        admin = ui.page("ADMIN")
        assert ui.w.top.group.button(ui.w.PAGES.index("ADMIN")).isVisible()
        session = ui.core.accounts.session
        user_id, temporary = ui.core.auth.create_user(session, "bob", "Bob")
        admin.refresh()
        assert until(lambda: admin.table.rowCount() == 2)
        admin.table.selectRow([u["username"] for u in admin._users].index("bob"))
        admin._reset()                                             # needs the owner's password again
        assert until(lambda: len(shown) == 1)
        bob = ui.core.auth.login("bob", shown[0])
        assert ui.core.auth.require(bob).must_change_password
        admin.table.selectRow([u["username"] for u in admin._users].index("bob"))
        admin._toggle_status()
        assert until(lambda: any(u["username"] == "bob" and u["status"] == "disabled" for u in admin._users))
        admin.fields["registration_enabled"].setChecked(True)
        admin._save_security()
        assert until(lambda: ui.core.settings.get("accounts", "registration_enabled") is True)
        admin.refresh_db(True)
        assert until(lambda: "integrity_check" in admin.db_view.toPlainText())
        assert "password" not in admin.db_view.toPlainText().lower()
    finally:
        ui.close()


def test_normal_user_sees_no_owner_pages(qapp, paths):
    first = UI(paths)
    first.core.auth.create_user(first.core.accounts.session, "bob", "Bob", "Kiwi-Lamp-456")
    first.close()
    ui = UI(paths, signed_in=False)
    try:
        ui.core.accounts.login("bob", "Kiwi-Lamp-456")
        login = ui.w.login
        assert until(lambda: login.stack.currentIndex() == 3)   # owner-set password must be replaced first
        assert ui.w.root_stack.currentIndex() == 0
        login.change_current.setText("Kiwi-Lamp-456")
        login.change_new.setText("Mango-Tree-789")
        login.change_confirm.setText("Mango-Tree-789")
        login._change_password()
        assert until(lambda: ui.w.root_stack.currentIndex() == 1)
        assert not ui.core.auth.require(ui.core.accounts.session).must_change_password
        visible = {name for i, name in enumerate(ui.w.PAGES) if ui.w.top.group.button(i).isVisible()}
        assert "ADMIN" not in visible and "SETTINGS" not in visible
        assert {"ASSISTANT", "HISTORY", "MEMORY", "ACCOUNT"} <= visible
        assert "BOB · USER" in ui.w.top.user.text()
    finally:
        ui.close()


def test_tasks_page_is_per_user(qapp, paths):
    ui = UI(paths)
    try:
        tasks = ui.page("TASKS")
        tasks.quick.setText("Owner task")
        tasks._quick_add()
        assert until(lambda: tasks.pending.rowCount() == 1)
        assert tasks.activity.operations >= 2
    finally:
        ui.close()
