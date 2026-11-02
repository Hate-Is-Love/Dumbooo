"""DUMBOO learns lasting facts from normal conversation and uses them later (ai/learning.py)."""

import json

import pytest

from ai import errors as E
from ai.learning import ConversationLearner
from tests.ai_fakes import ScriptedProvider
from tests.test_history_memory import Rig


class LearningProvider(ScriptedProvider):
    """Chat answers normally; answers the learning prompt with scripted facts."""

    def __init__(self, facts):
        super().__init__(answer="Nice!")
        self.facts = facts

    def generate_response(self, request, cancel=None):
        if "extract lasting facts" in request.system_prompt:
            self.requests.append(request)
            if isinstance(self.facts, Exception):
                raise self.facts
            from ai.base import AIResult
            return AIResult(json.dumps({"facts": self.facts}))
        return super().generate_response(request, cancel)


@pytest.fixture
def rig(paths):
    r = Rig(paths, provider=LearningProvider([
        {"key": "exam preparing for", "value": "GATE 2027", "category": "goal"},
        {"key": "favourite food", "value": "spicy paneer", "category": "preference"}]), learning=True)
    yield r
    r.close()


def test_lasting_facts_are_learned_and_used_in_later_conversations(rig):
    rig.reply("I'm preparing for GATE 2027 and honestly I love spicy paneer")
    memories = {m["memory_key"]: m for m in rig.core.memory.list(rig.session)}
    assert memories["exam preparing for"]["memory_value"] == "GATE 2027"
    assert memories["favourite food"]["verification_status"] == "inferred"  # clearly marked as learned
    assert memories["favourite food"]["confidence"] < 1.0
    rig.reply("suggest a dinner idea")
    chat = [r for r in rig.provider.requests if "extract lasting facts" not in r.system_prompt][-1]
    assert "spicy paneer" in chat.system_prompt  # used for future communication


def test_only_messages_about_the_user_are_sent_for_learning(rig):
    rig.reply("tell me a joke about cats")
    rig.reply("open youtube")
    assert not [r for r in rig.provider.requests if "extract lasting facts" in r.system_prompt]


def test_sensitive_facts_are_never_learned(paths):
    r = Rig(paths, provider=LearningProvider([{"key": "medical condition", "value": "diabetes", "category": "fact"},
                                              {"key": "password", "value": "hunter2", "category": "fact"}]),
            learning=True)
    try:
        r.reply("my doctor says I have diabetes and my password is hunter2 by the way")
        assert r.core.memory.list(r.session) == []
    finally:
        r.close()


def test_learning_can_be_switched_off(rig):
    policy = rig.core.memory_assistant._policy
    rig.core.memory_assistant._policy = lambda: {**policy(), "memory_ai_learning": False}
    rig.reply("I'm learning to play the guitar these days")
    assert rig.core.memory.list(rig.session) == []


def test_ai_failure_never_breaks_the_conversation(paths):
    r = Rig(paths, provider=LearningProvider(E.QuotaError(provider="groq")), learning=True)
    try:
        assert r.reply("I usually study late at night with lofi music") == "Nice!"
        assert r.core.memory.list(r.session) == []
    finally:
        r.close()


def test_learner_is_rate_limited_and_filters_commands():
    calls = []
    clock = [100.0]
    learner = ConversationLearner(lambda *a, **k: calls.append(1) or '{"facts": []}', min_interval_s=20,
                                  clock=lambda: clock[0])
    assert not learner.worth_learning("open notepad for me please")
    assert not learner.worth_learning("hi")
    learner.learn("I really enjoy building robots on weekends")
    learner.learn("I also like chess quite a lot")
    assert len(calls) == 1
    clock[0] += 21
    learner.learn("I also like chess quite a lot")
    assert len(calls) == 2


def test_misheard_speech_and_guessed_names_are_never_learned():
    from ai.learning import ConversationLearner, looks_misheard

    garbled = ("I am going to the Lorel I am going to the Lorel Hello, hello Hello I am my son, hello I am going "
               "to the Lorel Lorel, you are going to the Lorel")
    assert looks_misheard(garbled) and not ConversationLearner.worth_learning(garbled)
    assert not looks_misheard("I am preparing for my GATE exam and I study every morning at six")
    learner = ConversationLearner(lambda *a, **k: '{"facts": [{"key": "name", "value": "Priya", '
                                                  '"category": "identity"}, {"key": "hobby", "value": "chess", '
                                                  '"category": "preference"}]}')
    facts = learner.learn("I think Priya said I should play more chess on weekends")
    assert [f.key for f in facts] == ["hobby"]                 # a name only from "my name is …"
    learner = ConversationLearner(lambda *a, **k: '{"facts": [{"key": "name", "value": "Roshan", '
                                                  '"category": "identity"}]}')
    assert [f.value for f in learner.learn("my name is Roshan and I study engineering")] == ["Roshan"]
