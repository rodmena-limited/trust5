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

# Max events to drain from queue in one batch (prevents call_from_thread flood)
_BATCH_SIZE = 64


class Trust5App(App[None]):
    """Textual TUI application for live pipeline monitoring and event display."""

    CSS_PATH = "styles.tcss"
    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+q", "quit", "Quit"),
        ("c", "clear_log", "Clear Log"),
        ("s", "toggle_scroll", "Toggle Auto-Scroll"),
    ]
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

    # ─── Elapsed timer ─────────────────────────────────────────────────────────

    def _tick_elapsed(self) -> None:
        """Update elapsed display every second from a single workflow clock."""
        if self._workflow_start_time is not None and not self._workflow_ended:
            elapsed = time.monotonic() - self._workflow_start_time
            self._sb1.elapsed = self._format_elapsed(elapsed)
            self._sb1.refresh()

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        s = int(seconds)
        if s < 60:
            return f"{s}s"
        m, s = divmod(s, 60)
        if m < 60:
            return f"{m}m {s:02d}s"
        h, m = divmod(m, 60)
        return f"{h}h {m:02d}m"

    # ─── Background workers ──────────────────────────────────────────────────

    @work(thread=True)
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
            except (OSError, RuntimeError) as exc:  # DB poll error
                logger.debug("watch_workflow poll error: %s", exc)
            time.sleep(0.5)

    @work(thread=True)
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
            except (OSError, RuntimeError):  # event queue error
                logger.debug("consume_events error", exc_info=True)
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

    # ─── Event routing ───────────────────────────────────────────────────────

    def _route_batch(self, events: list[Event | None]) -> None:
        """Route a batch of events. One bad event won't kill the TUI.
        Uses batch_update() to prevent intermediate layout passes.
        Scroll-to-end is deferred here (once per batch) instead of per-write.
        """
        with self.batch_update():
            for event in events:
                if event is None:
                    continue
                try:
                    self._route_event(event)
                except (OSError, RuntimeError, KeyError, ValueError) as exc:  # TUI rendering error
                    logger.debug("TUI event routing error: %s", exc)
            # Coalesced repaint — one refresh per batch, not per property change.
            self._header.refresh()
            self._sb1.refresh()
            self._sb0.refresh()
        if not self._trust5_log._user_scrolled:
            self._trust5_log.scroll_end(animate=False)

    def _route_event(self, event: Event) -> None:
        """Dispatch a single event to the appropriate widget."""
        code = event.code
        content = event.msg
        kind = event.kind

        # ── Block events (accumulate and render as panels) ──
        if kind == K_BLOCK_START:
            self._trust5_log.write_event(event)
            return

        if kind == K_BLOCK_LINE:
            self._trust5_log.write_event(event)
            return

        if kind == K_BLOCK_END:
            self._trust5_log.write_event(event)
            return

        # ── Stream events (accumulate and write inline to log) ──
        if kind == K_STREAM_START:
            self._current_stream_label = event.label or "Streaming"
            self._current_stream_code = code
            self._trust5_log.write_stream_start(code, self._current_stream_label)
            if code == M.ATHK:
                self._sb1.waiting = False  # LLM responded — stop waiting
                self._sb1.thinking = True
            return

        if kind == K_STREAM_TOKEN:
            self._trust5_log.write_stream_token(content, code=code)
            return

        if kind == K_STREAM_END:
            self._trust5_log.write_stream_end()
            if self._current_stream_code == M.ATHK:
                self._sb1.thinking = False
            return

        # ── Pipeline header updates ──
        # Track actual stage progress and per-module phase completions.
        # WSTR is a workflow-level event and must NOT trigger stage matching.
        module = event.label or ""

        if code == M.WSTG:
            self._header.update_stage(content, "running")
            ref = content.lower()
            # When an implementer starts, the preceding test-writer is done.
            if "implement" in ref:
                if not self._setup_counted:
                    self._header.count_stage_done("setup")
                    self._setup_counted = True
                if module:
                    self._header.mark_module_done("write_tests", module)
                self._header.count_stage_done(f"write_tests:{module}")
            elif "test-writer" in ref or "test_writer" in ref:
                if not self._setup_counted:
                    self._header.count_stage_done("setup")
                    self._setup_counted = True
        elif code == M.WSUC:
            current = self._header.current_stage
            self._header.completed_stages = self._header.completed_stages | {current}
        elif code == M.WFAL:
            self._header.update_stage(content, "failed")

        # Non-agent tasks: validate, repair, quality.
        if code == M.VRUN:
            self._header.update_stage("validate", "running")
            # Validate starting means the implementer stage is done.
            if module:
                self._header.mark_module_done("implement", module)
                self._header.count_stage_done(f"implement:{module}")
            # Integration validate (empty module): no phantom "implement:" key

        elif code == M.VFAL:
            self._header.update_stage("validate", "failed")

        elif code == M.RSTR:
            self._header.update_stage("repair", "running")

        elif code == M.RSKP:
            # Repair skipped still counts as a completed stage.
            if module:
                self._header.count_stage_done(f"repair:{module}")
            else:
                self._header.count_stage_done("integration_repair")

        elif code == M.RJMP:
            # Repair completed and jumping back — count it.
            if module:
                self._header.count_stage_done(f"repair:{module}")
            else:
                self._header.count_stage_done("integration_repair")

        elif code == M.RFAL:
            self._header.update_stage("repair", "failed")

        elif code == M.QRUN:
            self._header.update_stage("quality", "running")

        elif code == M.QFAL:
            self._header.update_stage("quality", "failed")
            # Quality gate jumped back to repair — un-checkmark quality and review
            # so the header accurately reflects we're cycling back.
            self._header.update_stage("quality", "failed")
            self._header.update_stage("review", "failed")
            self._header.update_stage("repair", "running")
            self._sb1.stage_name = "quality → repair"

        # Per-stage success events.
        elif code == M.VPAS:
            self._header.update_stage("validate", "success")
            if module:
                self._header.mark_module_done("validate", module)
                # Validate passed = no repair needed = repair phase done too.
                self._header.mark_module_done("repair", module)
                self._header.count_stage_done(f"validate:{module}")
                self._header.count_stage_done(f"repair:{module}")
            else:
                # Integration stages use dedicated keys.
                self._header.count_stage_done("integration_validate")
                self._header.count_stage_done("integration_repair")

        elif code == M.QPAS:
            self._header.update_stage("quality", "success")
            self._header.count_stage_done("quality")

        # Code review events.
        elif code == M.RVST:
            self._header.update_stage("review", "running")
        elif code == M.RVFL:
            self._header.update_stage("review", "failed")
        elif code == M.RVPS:
            self._header.update_stage("review", "success")
            self._header.count_stage_done("review")

        # Loop workflow events (trust5 loop command).
        elif code == M.LSTR:
            self._sb1.stage_name = "loop"
        elif code == M.LITR:
            self._sb1.stage_name = f"loop {content}"
        elif code == M.LEND:
            self._sb1.stage_name = "loop complete"
        elif code == M.LERR:
            self._sb1.stage_name = "loop error"

        # ── Elapsed timer: start on first WSTR, freeze on terminal events ──
        if code == M.WSTR:
            if self._workflow_start_time is None:
                self._workflow_start_time = time.monotonic()
            self._workflow_ended = False
        elif code in (M.WSUC, M.WFAL, M.WTMO, M.WINT):
            self._workflow_ended = True
            # Clear transient agent state so the status bar reflects completion,
            # not the last agent's stale thinking/tool state.
            self._sb1.thinking = False
            self._sb1.waiting = False
            self._sb1.current_tool = ""
            _terminal_stage_names: dict[str, str] = {
                M.WSUC: "completed",
                M.WFAL: "failed",
                M.WTMO: "failed",
                M.WINT: "interrupted",
            }
            self._sb1.stage_name = _terminal_stage_names.get(code, "done")

        # ── Status bar routing ──
        self._update_status_bars(code, content)

        # ── Main log (skip status-bar-only noise) ──
        if code in STATUS_BAR_ONLY:
            return

        self._trust5_log.write_event(event)

    def _update_status_bars(self, code: str, content: str) -> None:
        """Parse event content into structured status bar properties."""
        try:
            if code == M.MMDL:
                kv = _parse_kv(content)
                model = kv.get("model", content)
                thinking = kv.get("thinking", "")
                if thinking:
                    self._sb0.model_name = f"{model} (think:{thinking})"
                else:
                    self._sb0.model_name = model
            elif code == M.MPRF:
                kv = _parse_kv(content)
                self._sb0.provider = kv.get("provider", content)
            elif code == M.MTKN:
                kv = _parse_kv(content)
                tok_in = int(kv.get("in", "0"))
                tok_out = int(kv.get("out", "0"))
                self._sb0.token_info = f"{_format_count(tok_in)} in / {_format_count(tok_out)} out"
            elif code == M.MCTX:
                kv = _parse_kv(content)
                remaining = int(kv.get("remaining", "0"))
                window = int(kv.get("window", "1"))
                pct_free = int((remaining / window) * 100) if window else 0
                self._sb0.context_info = f"ctx {pct_free}% free"
            elif code == M.FCHG:
                kv = _parse_kv(content)
                path = kv.get("path", "")
                if path:
                    self._changed_files.add(path)
                    self._sb1.files_changed = len(self._changed_files)
            elif code == M.SPRG:
                kv = _parse_kv(content)
                self._header.stage_total = int(kv.get("total", "0"))
                modules = int(kv.get("modules", "0"))
                if modules > 0:
                    self._header.module_count = modules
            elif code == M.WSTG:
                self._sb1.stage_name = content
            # SELP ignored — TUI drives its own elapsed timer via _tick_elapsed
            elif code == M.ATRN:
                # Content: "[name] Turn 3/20 (history=12 msgs)" → "Turn 3/20"
                m = re.search(r"Turn \d+/\d+", content)
                self._sb1.turn_info = m.group(0) if m else content
                # New turn starting → about to call LLM. Clear stale tool info
                # and show "generating" spinner so user knows the system is alive.
                self._sb1.current_tool = ""
                self._sb1.waiting = True
            elif code == M.CTLC:
                # LLM responded with a tool call — stop the waiting indicator
                self._sb1.waiting = False
                # Strip [agent] prefix, truncate for status bar
                display = content
                if "] " in display:
                    display = display.split("] ", 1)[1]
                self._sb1.current_tool = display[:60]
            elif code == M.ASUM:
                # Agent finished (final text response, no more tools)
                self._sb1.waiting = False
                self._sb1.current_tool = ""
        except (ValueError, KeyError):
            pass

    # ─── Actions ─────────────────────────────────────────────────────────────

    def action_clear_log(self) -> None:
        self._trust5_log.clear()

    def action_toggle_scroll(self) -> None:
        self._trust5_log._user_scrolled = not self._trust5_log._user_scrolled
        if not self._trust5_log._user_scrolled:
            self._trust5_log.scroll_end(animate=False)
        state = "ON" if not self._trust5_log._user_scrolled else "OFF (scroll up to read)"
        self.notify(f"Auto-scroll: {state}")
