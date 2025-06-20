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
