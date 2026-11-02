"""Strict per-user data isolation: A can never read, change, delete or export B's data."""

import pytest

from app.event_bus import Events
from services.auth_service import NotAuthenticated, PermissionDenied, Session
from services.history_service import HistoryNotFound, HistoryService
from services.memory_service import MemoryNotFound, MemoryService
from tests.fakes import RecordingBus, wait_for


@pytest.fixture
def svc(accounts):
    return HistoryService(accounts.db, accounts.auth), MemoryService(accounts.db, accounts.auth)


def seed(svc, accounts):
    history, memory = svc
    conv_a, msg_a = history.record_user_message(accounts.owner, None, "My bank is Zeta Bank", "text", "a-1")
    history.record_assistant_message(accounts.owner, msg_a, "Noted.", "fake", "m")
    mem_a = memory.remember(accounts.owner, "favourite colour", "teal", "preference").memory["id"]
    return conv_a, msg_a, mem_a


# ---------------------------------------------------------------- 10-12
def test_user_cannot_read_other_users_conversations(svc, accounts):
    history, _ = svc
    conv_a, _msg, _mem = seed(svc, accounts)
    assert history.list_conversations(accounts.user) == []
    assert history.search_messages(accounts.user, "Zeta bank") == []
    with pytest.raises(HistoryNotFound):
        history.get_conversation(accounts.user, conv_a)
    assert history.get_conversation(accounts.owner, conv_a)["messages"]


def test_user_cannot_read_other_users_memories(svc, accounts):
    _, memory = svc
    seed(svc, accounts)
    assert memory.list(accounts.user) == []
    assert memory.search(accounts.user, "favourite colour teal") == []
    assert memory.retrieve_for_prompt(accounts.user, "what is my favourite colour") == []
    assert memory.list(accounts.owner)[0]["memory_value"] == "teal"


def test_user_cannot_change_or_delete_other_users_records(svc, accounts):
    history, memory = svc
    conv_a, msg_a, mem_a = seed(svc, accounts)
    for action in (lambda: history.rename_conversation(accounts.user, conv_a, "pwned"),
                   lambda: history.set_archived(accounts.user, conv_a, True),
                   lambda: history.delete_conversation(accounts.user, conv_a),
                   lambda: history.record_assistant_message(accounts.user, msg_a, "injected", None, None)):
        with pytest.raises(HistoryNotFound):
            action()
    for action in (lambda: memory.update(accounts.user, mem_a, value="hacked"),
                   lambda: memory.forget(accounts.user, mem_a)):
        with pytest.raises(MemoryNotFound):
            action()
    history.mark_user_message(accounts.user, msg_a, "failed")  # silently matches nothing
    history.clear_history(accounts.user, confirmed=True)
    memory.clear_all(accounts.user, confirmed=True)
    conv = history.get_conversation(accounts.owner, conv_a)
    assert conv["title"] != "pwned" and conv["messages"][0]["status"] == "completed"
    assert memory.list(accounts.owner)[0]["memory_value"] == "teal"


def test_writing_into_another_users_conversation_is_refused(svc, accounts):
    history, _ = svc
    conv_a, _msg, _mem = seed(svc, accounts)
    with pytest.raises(HistoryNotFound):
        history.record_user_message(accounts.user, conv_a, "sneaky", "text", "b-1")
    with pytest.raises(Exception):  # and the database trigger refuses it even below the service layer
        accounts.db.execute("INSERT INTO messages (conversation_id, user_id, role, content, status, created_at, "
                            "updated_at) VALUES (?, ?, 'user', 'x', 'completed', 'now', 'now')",
                            (conv_a, accounts.user_id))


# ---------------------------------------------------------------- 13. no user-id bypass
def test_user_identity_always_comes_from_the_session(svc, accounts):
    history, memory = svc
    seed(svc, accounts)
    # The API takes no user id at all; display fields of a Session are ignored.
    forged = Session(accounts.user.token, username="alice", role="owner")
    assert history.list_conversations(forged) == []
    assert memory.list(forged) == []
    with pytest.raises(NotAuthenticated):
        memory.list(Session("guessed-token"))
    # A record id belonging to someone else cannot be used as a memory source.
    other_msg = accounts.db.scalar("SELECT id FROM messages WHERE user_id = ?", (accounts.owner_id,))
    created = memory.remember(accounts.user, "pet", "dog", source_message_id=other_msg).memory
    assert created["source_message_id"] is None
    memory.link_source(accounts.user, created["id"], other_msg)
    assert memory.list(accounts.user)[0]["source_message_id"] is None


# ---------------------------------------------------------------- 14. export
def test_export_contains_only_own_data_and_needs_permission(svc, accounts):
    history, memory = svc
    seed(svc, accounts)
    memory.remember(accounts.user, "pet", "dog")
    exported = history.export(accounts.user)
    text = str(exported)
    assert "Zeta" not in text and "teal" not in text and "dog" in text
    assert "password_hash" not in text and accounts.user.token not in text
    accounts.auth.set_permission(accounts.owner, accounts.user_id, "history.export", False)
    with pytest.raises(PermissionDenied):
        history.export(accounts.user)


# ---------------------------------------------------------------- 15. switching accounts clears context
def test_switching_accounts_clears_private_context(paths):
    from tests.ai_fakes import ScriptedProvider
    from tests.test_application import make_core

    provider = ScriptedProvider()
    core = make_core(paths, ai_provider_factory=lambda _c: provider)
    rec = RecordingBus(core.bus)
    core.start()
    try:
        core.bus.publish(Events.USER_TEXT, {"text": "my secret plan is Apollo", "source": "typed"})
        assert wait_for(lambda: rec.of(Events.RESPONSE_READY))
        assert core.conversation.context.turn_count == 1
        owner = core.accounts.session
        core.auth.create_user(owner, "bob", "Bob", "Kiwi-Lamp-456")
        core.accounts.login("bob", "Kiwi-Lamp-456")          # switch user
        assert wait_for(lambda: rec.of(Events.USER_SIGNED_IN) and len(rec.of(Events.USER_SIGNED_IN)) >= 2)
        assert core.conversation.context.turn_count == 0       # Alice's turns are gone from memory
        assert core.recorder.current_conversation(core.accounts.session) is None
        with pytest.raises(NotAuthenticated):
            core.auth.require(owner)                          # Alice's session was ended
        core.bus.publish(Events.USER_TEXT, {"text": "what is my secret plan?", "source": "typed"})
        assert wait_for(lambda: len(rec.of(Events.RESPONSE_READY)) >= 2)
        sent = provider.requests[-1]
        everything = sent.system_prompt + " ".join(m.content for m in sent.messages)
        assert "Apollo" not in everything                     # nothing of Alice reached Bob's AI request
        core.accounts.logout()
        assert core.conversation.context.turn_count == 0
    finally:
        core.shutdown()
