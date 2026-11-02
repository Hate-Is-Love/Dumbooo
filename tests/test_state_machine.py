import threading

from app.event_bus import EventBus, Events
from app.state_machine import TRANSITIONS, AssistantState as S, StateMachine
from tests.fakes import RecordingBus, wait_for


def make(initial=S.SLEEPING):
    bus = EventBus(synchronous=True)
    return StateMachine(bus, initial), RecordingBus(bus)


def test_allowed_transition_publishes_event():
    sm, rec = make()
    assert sm.transition(S.WAKING, "wake phrase")
    assert sm.state == S.WAKING
    assert rec.of(Events.STATE_CHANGED) == [{"old": "SLEEPING", "new": "WAKING", "reason": "wake phrase"}]


def test_illegal_transition_is_refused_without_event():
    sm, rec = make()
    assert not sm.transition(S.SPEAKING)
    assert sm.state == S.SLEEPING
    assert rec.of(Events.STATE_CHANGED) == []


def test_same_state_is_noop():
    sm, rec = make(S.IDLE)
    assert not sm.transition(S.IDLE)
    assert rec.events == []


def test_full_voice_cycle():
    sm, _ = make()
    for state in (S.WAKING, S.LISTENING, S.PROCESSING, S.SPEAKING, S.INTERRUPTED, S.LISTENING,
                  S.PROCESSING, S.SPEAKING, S.IDLE, S.SLEEPING):
        assert sm.transition(state), state


def test_shutdown_reachable_from_everywhere_and_final():
    for state in S:
        if state == S.SHUTTING_DOWN:
            continue
        sm, _ = make(state)
        assert sm.transition(S.SHUTTING_DOWN)
        for other in S:
            assert not sm.transition(other)
        sm.force(S.IDLE)
        assert sm.state == S.SHUTTING_DOWN


def test_every_state_has_transition_table():
    assert set(TRANSITIONS) == set(S)


def test_error_records_reason_and_can_recover():
    sm, _ = make(S.LISTENING)
    sm.transition(S.ERROR, "mic unplugged")
    assert sm.last_error == "mic unplugged"
    assert sm.transition(S.IDLE)


def test_force_bypasses_table():
    sm, rec = make(S.INTERRUPTED)
    sm.force(S.SLEEPING, "sleep requested")
    assert sm.state == S.SLEEPING
    assert rec.of(Events.STATE_CHANGED)[-1]["new"] == "SLEEPING"


def test_concurrent_transitions_stay_consistent():
    sm, rec = make(S.IDLE)
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        sm.transition(S.LISTENING)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sm.state == S.LISTENING
    assert len(rec.of(Events.STATE_CHANGED)) == 1  # exactly one thread won


def test_async_bus_delivers_in_order_and_survives_handler_errors():
    bus = EventBus()
    seen = []

    def bad(_n, _p):
        raise RuntimeError("boom")

    bus.subscribe("x", bad)
    bus.subscribe("x", lambda _n, p: seen.append(p["i"]))
    bus.start()
    for i in range(50):
        bus.publish("x", {"i": i})
    assert wait_for(lambda: len(seen) == 50)
    assert seen == list(range(50))
    bus.stop()
    assert not bus.running


def test_unsubscribe():
    bus = EventBus(synchronous=True)
    seen = []
    unsubscribe = bus.subscribe("x", lambda _n, _p: seen.append(1))
    bus.publish("x")
    unsubscribe()
    bus.publish("x")
    assert seen == [1]
