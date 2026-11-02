"""Conversation history and long-term memory through the real headless DUMBOO core."""

import threading
import time

import pytest

from ai import errors as AE
from app.event_bus import Events
from services.memory_service import MemoryRejected, NeedsConfirmation
from tests.ai_fakes import ScriptedProvider
from tests.fakes import RecordingBus, wait_for
from tests.test_application import OWNER, make_core


class Rig:
    def __init__(self, paths, provider=None, learning=False, **kw):
        self.provider = provider or ScriptedProvider()
        self.core = make_core(paths, ai_provider_factory=lambda _c: self.provider, **kw)
        if not learning:  # AI learning adds its own request after each answer; tested in test_learning.py
            self.core.memory_assistant.learner = None
        self.rec = RecordingBus(self.core.bus)
        self.core.start()

    @property
    def session(self):
        return self.core.accounts.session

    def say(self, text, source="typed", expect=Events.RESPONSE_READY):
        before = len(self.rec.of(expect))
        self.core.bus.publish(Events.USER_TEXT, {"text": text, "source": source})
        assert wait_for(lambda: len(self.rec.of(expect)) > before, 5), f"no {expect} for {text!r}"
        self.core.recorder.flush()
        return self.rec.of(expect)[-1]

    def reply(self, text, source="typed"):
        return self.say(text, source)["text"]

    def messages(self):
        convs = self.core.history.list_conversations(self.session)
        return self.core.history.get_conversation(self.session, convs[0]["id"])["messages"]

    def close(self):
        self.core.shutdown()


@pytest.fixture
def rig(paths):
    r = Rig(paths)
    yield r
    r.close()


# ---------------------------------------------------------------- 16-17. saving
def test_user_and_assistant_messages_are_saved(rig):
    rig.reply("Hello DUMBOO")
    msgs = rig.messages()
    assert [(m["role"], m["status"], m["content"]) for m in msgs] == [
        ("user", "completed", "Hello DUMBOO"), ("assistant", "completed", "Answer to: Hello DUMBOO.")]
    assert msgs[1]["provider"] == "fake" and msgs[1]["model"] == "fake-model"
    assert msgs[1]["reply_to"] == msgs[0]["id"]


def test_voice_and_text_use_the_signed_in_user_and_one_conversation(rig):
    rig.reply("first, typed")
    rig.reply("second, spoken", source="voice")
    msgs = rig.messages()
    assert [m["input_mode"] for m in msgs if m["role"] == "user"] == ["text", "voice"]
    owner_id = rig.core.auth.require(rig.session).id
    assert {owner_id} == {r["user_id"] for r in rig.core.db.query("SELECT user_id FROM messages")}
    assert len(rig.core.history.list_conversations(rig.session)) == 1


def test_streamed_answer_is_one_row(rig):
    rig.provider.pieces = [f"w{i} " for i in range(30)]
    rig.reply("long answer please")
    assistant = [m for m in rig.messages() if m["role"] == "assistant"]
    assert len(assistant) == 1 and assistant[0]["content"].startswith("w0 w1")


def test_nothing_is_saved_without_a_signed_in_user(paths):
    r = Rig(paths, signed_in=False)
    try:
        text = r.reply("hello?")
        assert "sign in" in text.lower()
        assert r.core.db.scalar("SELECT COUNT(*) FROM messages") == 0
        assert r.provider.requests == []  # the AI was not even asked
    finally:
        r.close()


# ---------------------------------------------------------------- 18. failed / interrupted
def test_failed_request_keeps_user_message_marked_failed(paths):
    r = Rig(paths, provider=ScriptedProvider(errors=[AE.QuotaError("quota used up")]))
    try:
        r.say("will fail", expect=Events.RESPONSE_FAILED)
        r.core.recorder.flush()
        msgs = r.messages()
        assert [(m["role"], m["status"], m["error_kind"]) for m in msgs] == [("user", "failed", "quota")]
    finally:
        r.close()


def test_partial_answer_is_saved_as_interrupted_not_completed(paths):
    provider = ScriptedProvider(pieces=["Part one. ", "Part two. ", "Part three."], errors=[AE.NetworkError()],
                                fail_after=2)
    r = Rig(paths, provider=provider)
    try:
        r.say("tell me", expect=Events.RESPONSE_FAILED)
        r.core.recorder.flush()
        msgs = r.messages()
        assert (msgs[0]["status"], msgs[1]["status"], msgs[1]["content"]) == ("failed", "interrupted",
                                                                              "Part one. Part two.")
    finally:
        r.close()


def test_cancelled_request_is_marked_interrupted(paths):
    gate = threading.Event()
    r = Rig(paths, provider=ScriptedProvider(gate=gate))
    try:
        r.core.bus.publish(Events.USER_TEXT, {"text": "slow one", "source": "typed"})
        assert wait_for(lambda: r.rec.of(Events.AI_STARTED))
        r.core.bus.publish(Events.STOP_SPEAKING, {"source": "ui"})
        assert wait_for(lambda: r.rec.of(Events.RESPONSE_CANCELLED))
        gate.set()
        time.sleep(0.2)
        r.core.recorder.flush()
        msgs = r.messages()
        assert [(m["role"], m["status"]) for m in msgs] == [("user", "interrupted")]
    finally:
        r.close()


# ---------------------------------------------------------------- 19. duplicates
def test_retry_reuses_the_original_message(paths):
    r = Rig(paths, provider=ScriptedProvider(errors=[AE.QuotaError("quota used up")]))
    try:
        r.say("retry me", expect=Events.RESPONSE_FAILED)
        r.core.bus.publish(Events.RETRY_REQUESTED, {})
        assert wait_for(lambda: r.rec.of(Events.RESPONSE_READY))
        r.core.recorder.flush()
        msgs = r.messages()
        assert [(m["role"], m["status"]) for m in msgs] == [("user", "completed"), ("assistant", "completed")]
    finally:
        r.close()


def test_saving_is_idempotent(rig):
    history, s = rig.core.history, rig.session
    c1, m1 = history.record_user_message(s, None, "same", "text", "key-1")
    c2, m2 = history.record_user_message(s, None, "same", "text", "key-1")
    a1 = history.record_assistant_message(s, m1, "answer", "p", "m")
    a2 = history.record_assistant_message(s, m1, "answer", "p", "m")
    assert (c1, m1, a1) == (c2, m2, a2)
    assert rig.core.db.scalar("SELECT COUNT(*) FROM messages") == 2


def test_empty_answer_is_never_fabricated(rig):
    history, s = rig.core.history, rig.session
    _c, m = history.record_user_message(s, None, "q", "text", "key-x")
    assert history.record_assistant_message(s, m, "   ", "p", "m") is None
    assert rig.core.db.scalar("SELECT COUNT(*) FROM messages WHERE role = 'assistant'") == 0


# ---------------------------------------------------------------- 20-21. persistence and search
def test_history_and_memory_survive_restart(paths):
    r = Rig(paths)
    r.reply("Remember that my project is called DUMBOO.")
    r.reply("The weather is nice in Pune today")
    r.close()
    r2 = Rig(paths, signed_in=False)
    try:
        assert not r2.core.accounts.signed_in  # a restart never grants access by itself
        r2.core.accounts.login(OWNER[0], OWNER[2])
        assert len(r2.core.history.list_conversations(r2.session)) == 1
        assert r2.core.memory.list(r2.session)[0]["memory_value"] == "DUMBOO"
        hits = r2.core.history.search_messages(r2.session, "weather Pune")
        assert hits and "weather" in hits[0]["content"]
    finally:
        r2.close()


def test_search_ranks_relevant_messages(rig):
    for text in ("I bought a red bicycle", "Python lists are mutable", "My bicycle needs new brakes"):
        rig.reply(text)
    hits = rig.core.history.search_messages(rig.session, "bicycle brakes")
    assert hits[0]["content"] == "My bicycle needs new brakes"
    assert all("Python" not in h["content"] for h in hits)
    assert rig.core.history.search_messages(rig.session, "the of and") == []  # stop-words only


def test_conversation_management(rig):
    rig.reply("first conversation")
    s, history = rig.session, rig.core.history
    cid = history.list_conversations(s)[0]["id"]
    history.rename_conversation(s, cid, "Renamed")
    history.set_archived(s, cid, True)
    assert history.list_conversations(s) == []
    assert history.list_conversations(s, include_archived=True)[0]["title"] == "Renamed"
    rig.core.bus.publish(Events.CONVERSATION_CLEARED, {})   # NEW CHAT starts a new conversation
    time.sleep(0.1)
    rig.reply("second conversation")
    assert len(history.list_conversations(s, include_archived=True)) == 2
    with pytest.raises(ValueError):
        history.clear_history(s)                       # must be confirmed
    assert history.clear_history(s, confirmed=True) == 2


def test_deleting_a_conversation_keeps_memories(rig):
    rig.reply("Remember that my favourite drink is chai")
    s = rig.session
    cid = rig.core.history.list_conversations(s)[0]["id"]
    rig.core.history.delete_conversation(s, cid)
    memory = rig.core.memory.list(s)[0]
    assert memory["memory_value"] == "chai" and memory["source_message_id"] is None


def test_retention_policy_purges_old_conversations(rig):
    rig.reply("old message")
    rig.core.db.execute("UPDATE conversations SET updated_at = '2020-01-01T00:00:00'")
    assert rig.core.history.purge_expired(30) == 1
    assert rig.core.history.purge_expired(0) == 0  # 0 = keep forever


# ---------------------------------------------------------------- 22-25. memory commands
def test_explicit_remember_creates_a_linked_record(rig):
    text = rig.reply("Remember that my project is called DUMBOO.")
    assert text == "Got it, Sir. I'll remember that your project is called DUMBOO."
    memory = rig.core.memory.list(rig.session)[0]
    assert (memory["memory_key"], memory["memory_value"], memory["category"]) == ("project", "DUMBOO", "project")
    assert memory["verification_status"] == "user_confirmed" and memory["source_message_id"] is not None
    assert rig.provider.requests == []  # handled locally: the AI never decides what is stored


def test_duplicate_memory_is_not_stored_twice(rig):
    rig.reply("Remember that my project is called DUMBOO.")
    assert "already have that saved" in rig.reply("remember my project is called dumboo")
    assert rig.core.db.scalar("SELECT COUNT(*) FROM long_term_memory") == 1


def test_correction_supersedes_old_fact(rig):
    rig.reply("Remember that my project is called DUMBOO.")
    assert rig.reply("Actually, my project is called Nova.").startswith("Updated, Sir")
    active = rig.core.memory.list(rig.session)
    assert [(m["memory_key"], m["memory_value"]) for m in active] == [("project", "Nova")]
    old = rig.core.memory.list(rig.session, include_superseded=True)
    assert {m["status"] for m in old} == {"active", "superseded"}
    assert rig.reply("Update my saved information: my project is called Orion").startswith("Updated")


def test_forget_commands(rig):
    rig.reply("Remember that my favourite colour is teal")
    rig.reply("Remember that my city is Pune")
    assert "forgotten your favourite colour" in rig.reply("Forget my favourite colour")
    assert [m["memory_key"] for m in rig.core.memory.list(rig.session)] == ["city"]
    rig.reply("Remember that my pet is a cat named Miso")
    assert rig.reply("forget that") == "Done. I've forgotten it."
    assert "don't have anything saved" in rig.reply("forget my spaceship")
    erased = rig.core.db.query("SELECT memory_key, memory_value FROM long_term_memory WHERE status = 'deleted'")
    assert all(r["memory_value"] == "" and r["memory_key"] == "(forgotten)" for r in erased)  # text really erased
    actions = [a["action"] for a in rig.core.memory.audit(rig.session)]
    assert actions.count("delete") == 2 and "create" in actions


def test_list_and_clear_with_confirmation(rig):
    assert "don't have anything saved" in rig.reply("What do you remember about me?")
    rig.reply("Remember that my project is called DUMBOO.")
    listing = rig.reply("Show my saved memories")
    assert "project" in listing and "DUMBOO" in listing
    assert "Are you sure" in rig.reply("Clear my memories")
    assert rig.reply("no") == "Okay, I won't change anything."
    assert len(rig.core.memory.list(rig.session)) == 1
    rig.reply("clear my memories")
    assert "erased" in rig.reply("yes")
    assert rig.core.memory.list(rig.session) == []


def test_sensitive_fact_needs_confirmation(rig):
    assert "sensitive" in rig.reply("Remember that my medication is Metformin")
    assert rig.core.memory.list(rig.session) == []
    assert rig.reply("yes").startswith("Got it")
    assert rig.core.memory.list(rig.session)[0]["sensitivity"] == "sensitive"


def test_call_me_sets_the_title(rig):
    assert rig.reply("Call me Captain") == "Understood. I'll call you Captain."
    assert rig.core.profile.get(rig.session)["title"] == "Captain"
    rig.reply("tell me a joke")  # ("what time is it?" is now answered locally, without the AI)
    assert 'Address the user as "Captain"' in rig.provider.requests[-1].system_prompt


def test_automatic_extraction_of_clear_statements_only(rig):
    rig.reply("I live in Pune. Maybe my favourite food is pizza. What is my name?")
    memories = rig.core.memory.list(rig.session)
    assert [(m["memory_key"], m["memory_value"], m["verification_status"]) for m in memories] == [
        ("home", "Pune", "user_stated")]
    assert memories[0]["confidence"] < 1.0


# ---------------------------------------------------------------- 26-29. retrieval and AI context
def test_retrieval_gives_the_ai_the_saved_fact_and_title(rig):
    rig.reply("Remember that my project is called DUMBOO.")
    rig.core.conversation.context.clear()  # prove the answer comes from the database, not the chat context
    rig.reply("What is my project called?")
    prompt = rig.provider.requests[-1].system_prompt
    assert "- project: DUMBOO (confirmed by the user" in prompt
    assert 'Address the user as "Sir"' in prompt
    retrieval = [p for p in rig.rec.of(Events.MEMORY_RETRIEVAL) if p["phase"] == "done"][-1]
    assert retrieval["memories"] >= 1


def test_missing_information_is_not_invented(rig):
    rig.reply("What is my favourite movie?")
    prompt = rig.provider.requests[-1].system_prompt
    assert "No saved facts matched this message." in prompt
    assert "say you don't have it saved; never guess personal facts" in prompt


def test_retrieval_only_returns_relevant_records(rig):
    for fact in ("my project is called DUMBOO", "my favourite colour is teal", "my car is a red Tesla"):
        rig.reply(f"remember {fact}")
    found = rig.core.memory.retrieve_for_prompt(rig.session, "what colour do I like? favourite colour", limit=1)
    assert [m["memory_key"] for m in found] == ["favourite colour"]


def test_other_users_memories_never_reach_the_ai(paths):
    r = Rig(paths)
    try:
        r.reply("Remember that my safe word is Pineapple")
        owner = r.session
        r.core.auth.create_user(owner, "bob", "Bob", "Kiwi-Lamp-456")
        r.core.accounts.login("bob", "Kiwi-Lamp-456")
        r.reply("What is my safe word? Pineapple?")
        sent = r.provider.requests[-1]
        context = sent.system_prompt.split("## Signed-in user")[1]
        assert "Pineapple" not in context and "Bob" in context
    finally:
        r.close()


def test_secrets_are_never_stored_or_sent(rig):
    assert "can't store passwords" in rig.reply("Remember that my wifi password is hunter2")
    assert "can't store passwords" in rig.reply("remember my api key is AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ012345")
    assert rig.core.memory.list(rig.session) == []
    with pytest.raises(MemoryRejected):
        rig.core.memory.remember(rig.session, "bank pin", "1234")
    with pytest.raises(NeedsConfirmation):
        rig.core.memory.remember(rig.session, "diagnosis", "asthma")
    rig.reply("my token is sk-abcdefghijklmnopqrstuvwx1234")  # saved to history, but…
    rig.reply("what is my token sk-abcdefghijklmnopqrstuvwx1234 for?")
    prompt = rig.provider.requests[-1].system_prompt
    assert "sk-abcdefghijklmnop" not in prompt.split("## Signed-in user")[1]  # …never retrieved into prompts


def test_memory_permission_switches_memory_off(paths):
    r = Rig(paths)
    try:
        owner = r.session
        user_id, _ = r.core.auth.create_user(owner, "bob", "Bob", "Kiwi-Lamp-456")
        r.core.auth.set_permission(owner, user_id, "memory.use", False)
        r.core.accounts.login("bob", "Kiwi-Lamp-456")
        assert "turned off" in r.reply("remember my car is blue")
        assert r.core.db.scalar("SELECT COUNT(*) FROM long_term_memory") == 0
    finally:
        r.close()


def test_ai_chat_permission_is_enforced(paths):
    r = Rig(paths)
    try:
        owner = r.session
        user_id, _ = r.core.auth.create_user(owner, "bob", "Bob", "Kiwi-Lamp-456")
        r.core.auth.set_permission(owner, user_id, "ai.chat", False)
        r.core.accounts.login("bob", "Kiwi-Lamp-456")
        assert "not allowed" in r.reply("hello")
        assert r.provider.requests == []
    finally:
        r.close()


def test_compound_statements_become_separate_facts(rig):
    rig.reply("I live in Pune and I work as an engineer.")
    assert sorted((m["memory_key"], m["memory_value"]) for m in rig.core.memory.list(rig.session)) == [
        ("home", "Pune"), ("job", "engineer")]
    text = rig.reply("Remember that my city is Pune and my car is a red Tesla")
    assert "your city is Pune and that your car is a red Tesla" in text
    assert rig.reply("remember I like tea and coffee").startswith("Got it")  # one fact, not split


def test_save_that_in_memory_saves_the_previous_message_not_these_words(rig):
    rig.reply("My college is MIT Pune")                                # goes to the (fake) AI
    reply = rig.reply("Save that in long-term memory, remember it every time when I log in.")
    values = [m["memory_value"] for m in rig.core.memory.list(rig.session)]
    assert "MIT Pune" in values and not any("long-term memory" in v for v in values)
    assert "remember" in reply.lower()


def test_a_fact_with_save_it_at_the_end_is_saved(rig):
    rig.reply("My favourite language is Python, save it in long-term memory.")
    values = [m["memory_value"] for m in rig.core.memory.list(rig.session)]
    assert "Python" in values
