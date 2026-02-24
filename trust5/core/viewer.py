"""StdoutViewer — built-in event consumer that renders to the terminal.

Consumes events from EventBus via an in-process queue and prints them
in the original trust5 format: ``{CODE}HH:MM:SS message``.

Runs in a daemon thread so a crash here NEVER kills the pipeline.
"""

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

    # ── lifecycle ─────────────────────────────────────────────────

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

    # ── render loop ───────────────────────────────────────────────

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
            except (OSError, ValueError, KeyError):  # render: I/O and data errors
                # Viewer crash must never propagate to pipeline
                logger.debug("StdoutViewer render error", exc_info=True)

    # ── rendering ─────────────────────────────────────────────────

    def _render(self, event: Event) -> None:
        tag = f"{{{event.code}}}"
        ts = event.ts

        # Intentional: StdoutViewer renders to terminal
        if event.kind == K_MSG:
            print(f"{tag}{ts} {event.msg}", flush=True)

        elif event.kind == K_BLOCK_START:
            print(f"{tag}{ts} \u250c\u2500\u2500 {event.label}", flush=True)

        elif event.kind == K_BLOCK_LINE:
            print(f"{tag}{ts}  \u2502 {event.msg}", flush=True)

        elif event.kind == K_BLOCK_END:
            print(f"{tag}{ts} \u2514\u2500\u2500", flush=True)

        elif event.kind == K_STREAM_START:
            print(f"{tag}{ts} {event.label}", end="", flush=True)

        elif event.kind == K_STREAM_TOKEN:
            sys.stdout.write(event.msg)
            sys.stdout.flush()

        elif event.kind == K_STREAM_END:
            print("", flush=True)
