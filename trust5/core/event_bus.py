"""IPC Event Bus for trust5.

Decouples the core pipeline from rendering via a non-blocking event bus.
Events are published to:
  1. In-process listener queues (for the built-in StdoutViewer).
  2. Connected Unix Domain Socket clients (for external TUI / watchers).

Design principles:
  - publish() NEVER blocks the pipeline (fire-and-forget).
  - Viewer crash does NOT affect the core process.
  - Socket path: <project>/.trust5/events.sock
"""

from __future__ import annotations

import collections
import json
import logging
import os
import queue
import socket
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── Event data model ─────────────────────────────────────────────────

# Event kinds (short keys for wire efficiency during streaming)
K_MSG = "msg"  # single-line message
K_BLOCK_START = "bs"  # block start (label)
K_BLOCK_LINE = "bl"  # block line (content)
K_BLOCK_END = "be"  # block end
K_STREAM_START = "ss"  # stream start (label)
K_STREAM_TOKEN = "st"  # stream token
K_STREAM_END = "se"  # stream end


@dataclass(frozen=True)
class Event:
    """Immutable event emitted by the pipeline."""

    kind: str  # K_MSG, K_BLOCK_START, etc.
    code: str  # M enum value (VRUN, QPAS, ...)
    ts: str  # HH:MM:SS
    msg: str = ""  # message text / token
    label: str = ""  # for block_start / stream_start

    def to_json(self) -> str:
        d: dict[str, str] = {"k": self.kind, "c": self.code, "t": self.ts}
        if self.msg:
            d["m"] = self.msg
        if self.label:
            d["l"] = self.label
        return json.dumps(d, ensure_ascii=False)


# Sentinel to signal listener shutdown
_SENTINEL: Event | None = None


# ── EventBus ─────────────────────────────────────────────────────────

_MAX_QUEUE = 10_000
_REPLAY_BUFFER_SIZE = 100  # Keep last N events for replay to new subscribers


class EventBus:
    """Non-blocking event bus with UDS broadcast and event replay."""

    def __init__(self, sock_path: str) -> None:
        self._sock_path = sock_path
        self._listeners: list[queue.Queue[Event | None]] = []
        self._listeners_lock = threading.Lock()
        self._clients: list[socket.socket] = []
        self._clients_lock = threading.Lock()
        self._server_sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._running = False
        # Circular buffer for event replay (deque is O(1) for append/popleft)
        self._replay_buffer: collections.deque[Event] = collections.deque(maxlen=_REPLAY_BUFFER_SIZE)
        self._replay_lock = threading.Lock()

    # ── lifecycle ─────────────────────────────────────────────────

    def start(self) -> None:
        """Start the UDS accept loop in a daemon thread."""
        if self._running:
            return
        self._running = True

        # Clean up stale socket
        if os.path.exists(self._sock_path):
            try:
                os.unlink(self._sock_path)
            except OSError:
                pass

        # Ensure parent directory exists
        sock_dir = os.path.dirname(self._sock_path)
        if sock_dir:
            os.makedirs(sock_dir, exist_ok=True)

        self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind(self._sock_path)
        # Restrict socket to owner only (prevent other local users from reading events)
        os.chmod(self._sock_path, 0o600)
        self._server_sock.listen(5)
        self._server_sock.settimeout(1.0)  # allows periodic shutdown check

        self._accept_thread = threading.Thread(target=self._accept_loop, name="event-bus-accept", daemon=True)
        self._accept_thread.start()
        logger.debug("EventBus started on %s", self._sock_path)

    def stop(self) -> None:
        """Shutdown: close all clients, close server socket, signal listeners."""
        if not self._running:
            return
        self._running = False

        # Signal all in-process listeners to stop
        with self._listeners_lock:
            for listener in self._listeners:
                try:
                    listener.put(_SENTINEL, timeout=5.0)
                except queue.Full:
                    pass

        # Close UDS clients
        with self._clients_lock:
            for client in self._clients:
                try:
                    client.close()
                except OSError:
                    pass
            self._clients.clear()

        # Close server socket
        if self._server_sock is not None:
            try:
                self._server_sock.close()
            except OSError:
                pass
            self._server_sock = None

        # Clean up socket file
        if os.path.exists(self._sock_path):
            try:
                os.unlink(self._sock_path)
            except OSError:
                pass

        logger.debug("EventBus stopped")

    # ── publish (fire-and-forget) ─────────────────────────────────

    def publish(self, event: Event) -> None:
        """Enqueue event to all listeners and broadcast to UDS clients.

        Never blocks. Drops silently if a listener queue is full.
        Also stores event in replay buffer for new subscribers.
        """
        # Store in replay buffer (deque with maxlen auto-evicts oldest)
        with self._replay_lock:
            self._replay_buffer.append(event)

        # In-process listeners (lock to prevent race with subscribe/unsubscribe)
        with self._listeners_lock:
            for listener in self._listeners:
                try:
                    listener.put_nowait(event)
                except queue.Full:
                    pass  # drop to protect pipeline throughput

        # UDS clients (best-effort)
        line = event.to_json() + "\n"
        data = line.encode("utf-8")
        dead: list[socket.socket] = []

        with self._clients_lock:
            for client in self._clients:
                try:
                    client.sendall(data)
                except (OSError, BrokenPipeError):
                    dead.append(client)

            for client in dead:
                try:
                    client.close()
                except OSError:
                    pass
                self._clients.remove(client)

    # ── in-process subscription ───────────────────────────────────

    def subscribe(self) -> queue.Queue[Event | None]:
        """Create an in-process event queue for a local consumer (e.g. StdoutViewer).

        Returns a queue that receives Events. A None sentinel signals shutdown.
        Replays recent events from buffer so late subscribers don't miss history.
        """
        q: queue.Queue[Event | None] = queue.Queue(maxsize=_MAX_QUEUE)

        # Replay recent events to new subscriber
        with self._replay_lock:
            for event in self._replay_buffer:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    break  # Stop replaying if queue is full

        with self._listeners_lock:
            self._listeners.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[Event | None]) -> None:
        """Remove a listener queue to prevent memory leaks."""
        with self._listeners_lock:
            try:
                self._listeners.remove(q)
            except ValueError:
                pass

    # ── UDS accept loop ───────────────────────────────────────────

    def _accept_loop(self) -> None:
        while self._running and self._server_sock is not None:
            try:
                conn, _ = self._server_sock.accept()
                conn.setblocking(True)
                with self._clients_lock:
                    self._clients.append(conn)
                logger.debug("EventBus: new client connected")
            except TimeoutError:
                continue
            except OSError:
                if self._running:
                    logger.debug("EventBus accept error (shutting down?)")
                break


# ── Module-level singleton ────────────────────────────────────────────

_bus: EventBus | None = None
_bus_lock = threading.Lock()


def init_bus(project_root: str) -> EventBus:
    """Initialize the module-level EventBus singleton and start it."""
    global _bus
    with _bus_lock:
        if _bus is not None:
            return _bus
        sock_path = os.path.join(project_root, ".trust5", "events.sock")
        _bus = EventBus(sock_path)
        _bus.start()
        return _bus


def get_bus() -> EventBus | None:
    """Return the current EventBus, or None if not initialized."""
    return _bus


def shutdown_bus() -> None:
    """Stop and clear the module-level EventBus."""
    global _bus
    with _bus_lock:
        if _bus is not None:
            _bus.stop()
            _bus = None
