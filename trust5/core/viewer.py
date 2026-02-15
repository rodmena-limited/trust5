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

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._render_loop, name="stdout-viewer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        # Put sentinel to unblock the queue.get()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    def _render_loop(self) -> None:
        """Main loop: pull events and render them."""
        while self._running:
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if event is None:
                break  # sentinel received

            try:
                self._render(event)
            except Exception:
                # Viewer crash must never propagate to pipeline
                logger.debug("StdoutViewer render error", exc_info=True)
