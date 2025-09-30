"""Tests for trust5.core.event_bus — EventBus, Event, and helper functions."""

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

# ── Fixtures ────────────────────────────────────────────────────────────

# macOS AF_UNIX paths are limited to 104 bytes.  pytest's tmp_path often
# exceeds that, so we create a short temp dir under /tmp for any test that
# calls start() (which binds a UDS).


@pytest.fixture
def short_tmp() -> Path:
    """Yield a short temp directory suitable for UDS paths, cleaned up after the test."""
    d = tempfile.mkdtemp(prefix="t5_", dir="/tmp")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def bus(short_tmp: Path) -> EventBus:
    """Create an EventBus backed by a short temp socket path; stop it after the test."""
    sock = str(short_tmp / "t.sock")
    b = EventBus(sock)
    yield b  # type: ignore[misc]
    b.stop()


def _make_event(msg: str = "hello", kind: str = K_MSG, code: str = "TEST") -> Event:
    return Event(kind=kind, code=code, ts="12:00:00", msg=msg)


# ── Event dataclass ─────────────────────────────────────────────────────


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


# ── publish / subscribe ─────────────────────────────────────────────────


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


# ── Replay buffer ────────────────────────────────────────────────────────


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


# ── subscribe / unsubscribe ─────────────────────────────────────────────


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


# ── Thread safety ────────────────────────────────────────────────────────


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


# ── stop() behaviour ────────────────────────────────────────────────────


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


# ── start / stop lifecycle ───────────────────────────────────────────────


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


def test_start_idempotent(short_tmp: Path) -> None:
    """Calling start() twice does not create duplicate accept threads."""
    sock = str(short_tmp / "idem.sock")
    b = EventBus(sock)
    try:
        b.start()
        thread1 = b._accept_thread
        b.start()
        thread2 = b._accept_thread
        assert thread1 is thread2
    finally:
        b.stop()


def test_start_cleans_stale_socket(short_tmp: Path) -> None:
    """start() removes a stale socket file before binding."""
    sock = str(short_tmp / "stale.sock")

    # Create a stale file at the socket path
    with open(sock, "w") as f:
        f.write("stale")
    assert os.path.exists(sock)

    b = EventBus(sock)
    try:
        b.start()
        # Should have replaced the stale file with a real socket
        assert os.path.exists(sock)
        assert b._running
    finally:
        b.stop()


# ── Socket permissions ───────────────────────────────────────────────────


def test_socket_permissions(short_tmp: Path) -> None:
    """After start(), the socket file has 0o600 permissions (owner-only)."""
    sock = str(short_tmp / "perm.sock")
    b = EventBus(sock)
    try:
        b.start()
        mode = os.stat(sock).st_mode
        # Check that the permission bits are exactly 0o600
        assert stat.S_IMODE(mode) == 0o600
    finally:
        b.stop()


# ── UDS client integration ──────────────────────────────────────────────


def test_uds_client_receives_events(short_tmp: Path) -> None:
    """A UDS client connected to the socket receives published events as JSON lines."""
    sock_path = str(short_tmp / "uds.sock")
    b = EventBus(sock_path)
    b.start()
    try:
        # Connect a client
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(sock_path)
        client.settimeout(2.0)

        # Give the accept loop time to register the client
        time.sleep(0.1)

        evt = Event(kind=K_MSG, code="TEST", ts="09:00:00", msg="uds-test")
        b.publish(evt)

        data = client.recv(4096).decode("utf-8")
        client.close()

        lines = [line for line in data.strip().split("\n") if line]
        assert len(lines) >= 1
        d = json.loads(lines[0])
        assert d["k"] == K_MSG
        assert d["m"] == "uds-test"
    finally:
        b.stop()


def test_dead_uds_client_removed(short_tmp: Path) -> None:
    """When a UDS client disconnects, publish removes it from the client list."""
    sock_path = str(short_tmp / "dead.sock")
    b = EventBus(sock_path)
    b.start()
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(sock_path)

        # Let the accept loop register the client
        time.sleep(0.1)
        assert len(b._clients) == 1

        # Disconnect the client
        client.close()

        # Publish should detect the dead client and remove it
        b.publish(_make_event("after-disconnect"))

        # Client list should be cleaned up
        assert len(b._clients) == 0
    finally:
        b.stop()


# ── Module-level helpers ─────────────────────────────────────────────────


def test_init_bus_and_shutdown(short_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """init_bus() creates a singleton, get_bus() returns it, shutdown_bus() clears it."""
    import trust5.core.event_bus as eb_mod

    # Reset the module-level singleton to avoid interference
    monkeypatch.setattr(eb_mod, "_bus", None)

    project_root = str(short_tmp)
    bus_instance = eb_mod.init_bus(project_root)

    assert bus_instance is not None
    assert eb_mod.get_bus() is bus_instance

    # Calling init_bus again returns the same instance
    same = eb_mod.init_bus(project_root)
    assert same is bus_instance

    eb_mod.shutdown_bus()
    assert eb_mod.get_bus() is None


def test_get_bus_returns_none_before_init(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_bus() returns None when init_bus has not been called."""
    import trust5.core.event_bus as eb_mod

    monkeypatch.setattr(eb_mod, "_bus", None)
    assert eb_mod.get_bus() is None


def test_shutdown_bus_noop_when_not_initialized(monkeypatch: pytest.MonkeyPatch) -> None:
    """shutdown_bus() is a no-op when no bus has been initialized."""
    import trust5.core.event_bus as eb_mod

    monkeypatch.setattr(eb_mod, "_bus", None)
    # Should not raise
    eb_mod.shutdown_bus()
