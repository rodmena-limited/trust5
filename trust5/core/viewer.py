from __future__ import annotations
import logging
import queue
import sys
import threading
from .event_bus import (
    K_BLOCK_END,
    K_BLOCK_LINE,
    K_BLOCK_START,
    K_MSG,
    K_STREAM_END,
    K_STREAM_START,
    K_STREAM_TOKEN,
    Event,
    EventBus,
)
logger = logging.getLogger(__name__)

class StdoutViewer:
    """Renders pipeline events to stdout."""
    def __init__(self, bus: EventBus) -> None:
        self._queue: queue.Queue[Event | None] = bus.subscribe()
        self._thread: threading.Thread | None = None
        self._running = False
