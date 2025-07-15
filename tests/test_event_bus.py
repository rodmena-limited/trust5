from __future__ import annotations
import json
import os
import queue
import shutil
import socket
import stat
import tempfile
import threading
import time
from pathlib import Path
import pytest
from trust5.core.event_bus import (
    _MAX_QUEUE,
    _REPLAY_BUFFER_SIZE,
    K_BLOCK_START,
    K_MSG,
    Event,
    EventBus,
)

def short_tmp() -> Path:
    """Yield a short temp directory suitable for UDS paths, cleaned up after the test."""
    d = tempfile.mkdtemp(prefix="t5_", dir="/tmp")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)

def bus(short_tmp: Path) -> EventBus:
    """Create an EventBus backed by a short temp socket path; stop it after the test."""
    sock = str(short_tmp / "t.sock")
    b = EventBus(sock)
    yield b  # type: ignore[misc]
    b.stop()

def _make_event(msg: str = "hello", kind: str = K_MSG, code: str = "TEST") -> Event:
    return Event(kind=kind, code=code, ts="12:00:00", msg=msg)

def test_event_to_json() -> None:
    """Event.to_json() produces correct compact JSON with the expected keys."""
    evt = Event(kind=K_MSG, code="VRUN", ts="10:30:45", msg="running tests", label="")
    raw = evt.to_json()
    d = json.loads(raw)

    assert d["k"] == K_MSG
    assert d["c"] == "VRUN"
    assert d["t"] == "10:30:45"
    assert d["m"] == "running tests"
    # Empty label should be omitted
    assert "l" not in d

def test_event_to_json_with_label() -> None:
    """When label is set, it appears in the JSON output under key 'l'."""
    evt = Event(kind=K_BLOCK_START, code="BSTART", ts="11:00:00", msg="", label="build")
    d = json.loads(evt.to_json())

    assert d["k"] == K_BLOCK_START
    assert d["l"] == "build"
    # Empty msg should be omitted
    assert "m" not in d

def test_event_to_json_minimal() -> None:
    """An event with no msg or label produces only k/c/t keys."""
    evt = Event(kind=K_MSG, code="X", ts="00:00:00")
    d = json.loads(evt.to_json())
    assert set(d.keys()) == {"k", "c", "t"}

def test_event_frozen() -> None:
    """Event is a frozen dataclass; attribute assignment should raise."""
    evt = _make_event()
    with pytest.raises(AttributeError):
        evt.msg = "modified"  # type: ignore[misc]

def test_publish_to_subscriber(bus: EventBus) -> None:
    """A subscriber receives events published after subscription."""
    q = bus.subscribe()
    evt = _make_event("test message")
    bus.publish(evt)

    received = q.get(timeout=1.0)
    assert received is not None
    assert received.msg == "test message"
    assert received.kind == K_MSG

def test_publish_multiple_subscribers(bus: EventBus) -> None:
    """All subscribers receive the same published event."""
    q1 = bus.subscribe()
    q2 = bus.subscribe()

    evt = _make_event("broadcast")
    bus.publish(evt)

    assert q1.get(timeout=1.0) == evt
    assert q2.get(timeout=1.0) == evt

def test_publish_drops_when_queue_full(bus: EventBus) -> None:
    """When a listener queue is full, publish drops the event silently (no block)."""
    q = bus.subscribe()

    # Fill the queue to capacity
    for i in range(_MAX_QUEUE):
        bus.publish(_make_event(f"fill-{i}"))

    assert q.full()

    # Publishing one more should not block or raise
    start = time.monotonic()
    bus.publish(_make_event("overflow"))
    elapsed = time.monotonic() - start

    # Should return almost instantly (non-blocking)
    assert elapsed < 1.0

    # Queue size should still be _MAX_QUEUE (overflow was dropped)
    assert q.qsize() == _MAX_QUEUE

def test_replay_buffer_on_subscribe(bus: EventBus) -> None:
    """A late subscriber receives replayed events from the buffer."""
    events = [_make_event(f"event-{i}") for i in range(5)]
    for evt in events:
        bus.publish(evt)

    # Subscribe after publishing — should get replay of all 5
    q = bus.subscribe()
    replayed = []
    while not q.empty():
        replayed.append(q.get_nowait())

    assert len(replayed) == 5
    assert [e.msg for e in replayed] == [f"event-{i}" for i in range(5)]

def test_replay_buffer_maxlen(bus: EventBus) -> None:
    """Replay buffer is capped at _REPLAY_BUFFER_SIZE; oldest events are evicted."""
    total = _REPLAY_BUFFER_SIZE + 50
    for i in range(total):
        bus.publish(_make_event(f"evt-{i}"))

    q = bus.subscribe()
    replayed = []
    while not q.empty():
        replayed.append(q.get_nowait())

    assert len(replayed) == _REPLAY_BUFFER_SIZE
    # The earliest retained event should be evt-50 (first 50 evicted)
    assert replayed[0].msg == f"evt-{total - _REPLAY_BUFFER_SIZE}"
    assert replayed[-1].msg == f"evt-{total - 1}"

def test_replay_does_not_duplicate_for_existing_subscriber(bus: EventBus) -> None:
    """An existing subscriber does not get replayed events again; only new ones."""
    q = bus.subscribe()

    bus.publish(_make_event("first"))
    bus.publish(_make_event("second"))

    items = []
    while not q.empty():
        items.append(q.get_nowait())

    # Should only have the two events, no replay duplication
    assert len(items) == 2

def test_subscribe_unsubscribe(bus: EventBus) -> None:
    """After unsubscribing, the queue no longer receives new events."""
    q = bus.subscribe()
    bus.unsubscribe(q)

    bus.publish(_make_event("after-unsub"))

    # Queue should remain empty (no new events delivered)
    assert q.empty()

def test_unsubscribe_nonexistent_queue(bus: EventBus) -> None:
    """Unsubscribing a queue that was never subscribed does not raise."""
    orphan: queue.Queue[Event | None] = queue.Queue()
    # Should not raise
    bus.unsubscribe(orphan)

def test_thread_safety_publish_subscribe(bus: EventBus) -> None:
    """Concurrent publish and subscribe/unsubscribe from multiple threads does not crash."""
    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def publisher(count: int) -> None:
        try:
            barrier.wait(timeout=5.0)
            for i in range(count):
                bus.publish(_make_event(f"thread-{threading.current_thread().name}-{i}"))
        except Exception as exc:
            errors.append(exc)

    def subscriber_cycle(count: int) -> None:
        try:
            barrier.wait(timeout=5.0)
            for _ in range(count):
                q = bus.subscribe()
                # Briefly read some events
                try:
                    q.get(timeout=0.01)
                except queue.Empty:
                    pass
                bus.unsubscribe(q)
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=publisher, args=(200,), name="pub-1"),
        threading.Thread(target=publisher, args=(200,), name="pub-2"),
        threading.Thread(target=subscriber_cycle, args=(50,), name="sub-1"),
        threading.Thread(target=subscriber_cycle, args=(50,), name="sub-2"),
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert errors == [], f"Thread safety errors: {errors}"

def test_stop_sends_sentinel(bus: EventBus) -> None:
    """stop() sends None sentinel to all listener queues."""
    bus.start()
    q = bus.subscribe()

    bus.stop()

    sentinel = q.get(timeout=2.0)
    assert sentinel is None

def test_stop_idempotent(bus: EventBus) -> None:
    """Calling stop() twice does not raise or cause errors."""
    bus.start()
    bus.stop()
    # Second stop should be a no-op
    bus.stop()

def test_stop_without_start(bus: EventBus) -> None:
    """Calling stop() on a bus that was never started does not raise."""
    bus.stop()

def test_start_stop_lifecycle(short_tmp: Path) -> None:
    """start() creates the socket, sets _running; stop() tears it down."""
    sock = str(short_tmp / "lc.sock")
    b = EventBus(sock)

    assert not b._running

    b.start()
    assert b._running
    assert os.path.exists(sock)
    assert b._server_sock is not None

    b.stop()
    assert not b._running
    assert b._server_sock is None
    # Socket file should be cleaned up
    assert not os.path.exists(sock)
