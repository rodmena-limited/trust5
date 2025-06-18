import logging
import queue
import re
import time
from typing import Any
from textual import work
from textual.app import App, ComposeResult
from textual.worker import get_current_worker
from ..core.event_bus import (
    K_BLOCK_END,
    K_BLOCK_LINE,
    K_BLOCK_START,
    K_STREAM_END,
    K_STREAM_START,
    K_STREAM_TOKEN,
    Event,
)
from ..core.message import M
from .widgets import (
    STATUS_BAR_ONLY,
    HeaderWidget,
    StatusBar0,
    StatusBar1,
    Trust5Log,
    _format_count,
    _parse_kv,
)
logger = logging.getLogger(__name__)
_BATCH_SIZE = 64

class Trust5App(App[None]):
    CSS_PATH = 'styles.tcss'
    BINDINGS = [('ctrl+c', 'quit', 'Quit'), ('ctrl+q', 'quit', 'Quit'), ('c', 'clear_log', 'Clear Log'), ('s', 'toggle_scroll', 'Toggle Auto-Scroll')]
    ENABLE_COMMAND_PALETTE = False
    def __init__(
        self,
        event_queue: queue.Queue[Event | None],
        store: Any = None,
        workflow_id: str = "",
    ) -> None:
        super().__init__()
        self.event_queue = event_queue
        self.store = store
        self.workflow_id = workflow_id
        self._current_stream_label = ""
        self._current_stream_code = ""
        self._changed_files: set[str] = set()
        self._workflow_result: Any = None
        # Elapsed timer: TUI-driven, independent of per-task SELP events
        self._workflow_start_time: float | None = None
        self._workflow_ended = False
        # Track setup completion (once) for progress counter
        self._setup_counted = False
        # Widget refs cached in on_mount
        self._trust5_log: Trust5Log
        self._header: HeaderWidget
        self._sb0: StatusBar0
        self._sb1: StatusBar1

    def compose(self) -> ComposeResult:
        yield HeaderWidget()
        yield Trust5Log(markup=False, max_lines=5000, auto_scroll=False, wrap=True)
        yield StatusBar1()
        yield StatusBar0()

    def on_mount(self) -> None:
        self._trust5_log = self.query_one(Trust5Log)
        self._header = self.query_one(HeaderWidget)
        self._sb0 = self.query_one(StatusBar0)
        self._sb1 = self.query_one(StatusBar1)
        self.set_interval(1.0, self._tick_elapsed)
        self.consume_events()
        if self.store and self.workflow_id:
            self.watch_workflow()

    def _tick_elapsed(self) -> None:
        """Update elapsed display every second from a single workflow clock."""
        if self._workflow_start_time is not None and not self._workflow_ended:
            elapsed = time.monotonic() - self._workflow_start_time
            self._sb1.elapsed = self._format_elapsed(elapsed)

    def _format_elapsed(seconds: float) -> str:
        s = int(seconds)
        if s < 60:
            return f"{s}s"
        m, s = divmod(s, 60)
        if m < 60:
            return f"{m}m {s:02d}s"
        h, m = divmod(m, 60)
        return f"{h}h {m:02d}m"

    def watch_workflow(self) -> None:
        """Poll workflow status and store result when terminal.

        The TUI stays open — the user decides when to quit (q / Ctrl+C).
        """
        import time

        from stabilize.models.status import WorkflowStatus

        terminal_statuses = {
            WorkflowStatus.SUCCEEDED,
            WorkflowStatus.FAILED_CONTINUE,
            WorkflowStatus.TERMINAL,
            WorkflowStatus.CANCELED,
        }

        while True:
            try:
                wf = self.store.retrieve(self.workflow_id)
                if wf.status in terminal_statuses:
                    time.sleep(0.5)  # let events drain
                    self._workflow_result = wf
                    # Safety net: clear status bar in case terminal events
                    # were missed (e.g. agent killed mid-turn without ASUM).
                    self.call_from_thread(self._clear_status_bar_on_completion, wf.status)
                    break
            except Exception as exc:
                logger.debug("watch_workflow poll error: %s", exc)
            time.sleep(0.5)

    def consume_events(self) -> None:
        """Background worker: drain events in batches and dispatch to main thread.

        When the event queue sends None (pipeline done), we stop consuming
        but do NOT exit the TUI — the user quits when ready.

        Resilience: transient errors don't kill the consumer. Only 10
        consecutive failures (or a cancelled worker) cause the loop to stop.
        """
        worker = get_current_worker()
        consecutive_errors = 0
        while not worker.is_cancelled:
            try:
                event = self.event_queue.get(timeout=0.1)
                consecutive_errors = 0
            except queue.Empty:
                continue
            except Exception:
                consecutive_errors += 1
                if consecutive_errors >= 10:
                    logger.debug("consume_events: %d consecutive errors, stopping", consecutive_errors)
                    break
                continue

            if event is None:
                break

            # Drain up to _BATCH_SIZE more without blocking
            batch = [event]
            done = False
            for _ in range(_BATCH_SIZE - 1):
                try:
                    ev = self.event_queue.get_nowait()
                    if ev is None:
                        done = True
                        break
                    batch.append(ev)
                except queue.Empty:
                    break

            self.call_from_thread(self._route_batch, batch)
            if done:
                break

    def _clear_status_bar_on_completion(self, status: Any) -> None:
        """Reset status bar to reflect pipeline completion.

        Called from both event routing (WFAL/WSUC) and watch_workflow
        (safety net for missed events).
        """
        self._sb1.thinking = False
        self._sb1.waiting = False
        self._sb1.current_tool = ""
        status_name = status.name if hasattr(status, "name") else str(status)
        if status_name in ("SUCCEEDED", "COMPLETED"):
            self._sb1.stage_name = "completed"
        elif status_name in ("CANCELED",):
            self._sb1.stage_name = "interrupted"
        else:
            self._sb1.stage_name = "failed"
        self._workflow_ended = True
