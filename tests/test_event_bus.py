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
