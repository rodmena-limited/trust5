import logging
import queue
import re
import time
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
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
from .sidebar import Sidebar, SidebarInfo, WatchdogLog
from .widgets import (
    STATUS_BAR_ONLY,
    HeaderWidget,
    StatusBar1,
    Trust5Log,
    _format_count,
    _parse_kv,
)

logger = logging.getLogger(__name__)

_BATCH_SIZE = 64

_WATCHDOG_BLOCK_CODES = frozenset({M.WDWN, M.WDER})
_WATCHDOG_SIDEBAR_CODES = frozenset({M.WDST, M.WDOK, M.WDWN, M.WDER})

_TOOL_DISPLAY_NAMES: dict[str, str] = {
    M.TBSH: "Bash",
    M.TWRT: "Write",
    M.TRED: "Read",
    M.TEDT: "Edit",
    M.TGLB: "Glob",
    M.TGRP: "Grep",
    M.TPKG: "Pkg",
    M.TINI: "Init",
}


class Trust5App(App[None]):
    """Textual TUI for live pipeline monitoring."""

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
        self._workflow_start_time: float | None = None
        self._workflow_ended = False
        self._setup_counted = False
        self._wd_block_buffer: list[str] = []
        self._wd_block_code: str = ""
        self._in_wd_block: bool = False
        self._trust5_log: Trust5Log
        self._header: HeaderWidget
        self._sidebar_info: SidebarInfo
        self._watchdog_log: WatchdogLog
        self._sb1: StatusBar1

    def compose(self) -> ComposeResult:
        yield HeaderWidget()
        with Horizontal(id="content-area"):
            yield Trust5Log(markup=False, max_lines=5000, auto_scroll=False, wrap=True)
            yield Sidebar(id="sidebar")
        yield StatusBar1()

    def on_mount(self) -> None:
        self._trust5_log = self.query_one(Trust5Log)
        self._header = self.query_one(HeaderWidget)
        self._sidebar_info = self.query_one(SidebarInfo)
        self._watchdog_log = self.query_one(WatchdogLog)
        self._sb1 = self.query_one(StatusBar1)
        self.set_interval(1.0, self._tick_elapsed)
        self.consume_events()
        if self.store and self.workflow_id:
            self.watch_workflow()

    # ─── Elapsed timer ─────────────────────────────────────────────────────────

    def _tick_elapsed(self) -> None:
        if self._workflow_start_time is not None and not self._workflow_ended:
            elapsed = time.monotonic() - self._workflow_start_time
            self._sidebar_info.elapsed = self._format_elapsed(elapsed)
            self._sidebar_info.refresh()

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
        """Poll workflow status until terminal, then record result."""
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
                    time.sleep(0.5)
                    self._workflow_result = wf
                    self.call_from_thread(self._clear_status_on_completion, wf.status)
                    break
            except (OSError, RuntimeError) as exc:
                logger.debug("watch_workflow poll error: %s", exc)
            time.sleep(0.5)

    @work(thread=True)
    def consume_events(self) -> None:
        """Drain event queue in batches, dispatch to main thread."""
        worker = get_current_worker()
        consecutive_errors = 0
        while not worker.is_cancelled:
            try:
                event = self.event_queue.get(timeout=0.1)
                consecutive_errors = 0
            except queue.Empty:
                continue
            except (OSError, RuntimeError):
                logger.debug("consume_events error", exc_info=True)
                consecutive_errors += 1
                if consecutive_errors >= 10:
                    logger.debug("consume_events: %d consecutive errors, stopping", consecutive_errors)
                    break
                continue

            if event is None:
                break

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

    def _clear_status_on_completion(self, status: Any) -> None:
        self._sidebar_info.thinking = False
        self._sidebar_info.waiting = False
        self._sb1.current_tool = ""
        status_name = status.name if hasattr(status, "name") else str(status)
        _terminal_names: dict[str, str] = {
            "SUCCEEDED": "completed",
            "COMPLETED": "completed",
            "CANCELED": "interrupted",
        }
        self._sb1.stage_name = _terminal_names.get(status_name, "failed")
        self._workflow_ended = True
        self._workflow_ended = True

    # ─── Event routing ───────────────────────────────────────────────────────

    def _route_batch(self, events: list[Event | None]) -> None:
        with self.batch_update():
            for event in events:
                if event is None:
                    continue
                try:
                    self._route_event(event)
                except (OSError, RuntimeError, KeyError, ValueError) as exc:
                    logger.debug("TUI event routing error: %s", exc)
            self._header.refresh()
            self._sb1.refresh()
            self._sidebar_info.refresh()
        if not self._trust5_log._user_scrolled:
            self._trust5_log.scroll_end(animate=False)

    def _route_event(self, event: Event) -> None:
        code = event.code
        content = event.msg
        kind = event.kind

        if kind == K_BLOCK_START:
            if code in _WATCHDOG_BLOCK_CODES:
                self._wd_block_buffer = []
                self._wd_block_code = code
                self._in_wd_block = True
                return
            self._trust5_log.write_event(event)
            return

        if kind == K_BLOCK_LINE:
            if self._in_wd_block:
                self._wd_block_buffer.append(content)
                return
            self._trust5_log.write_event(event)
            return

        if kind == K_BLOCK_END:
            if self._in_wd_block:
                narrative = "\n".join(self._wd_block_buffer)
                level = "error" if self._wd_block_code == M.WDER else "warn"
                self._watchdog_log.add_narrative(narrative, level)
                self._in_wd_block = False
                return
            self._trust5_log.write_event(event)
            return

        if kind == K_STREAM_START:
            self._current_stream_label = event.label or "Streaming"
            self._current_stream_code = code
            self._trust5_log.write_stream_start(code, self._current_stream_label)
            if code == M.ATHK:
                self._sidebar_info.waiting = False
                self._sidebar_info.thinking = True
            return

        if kind == K_STREAM_TOKEN:
            self._trust5_log.write_stream_token(content, code=code)
            return

        if kind == K_STREAM_END:
            self._trust5_log.write_stream_end()
            if self._current_stream_code == M.ATHK:
                self._sidebar_info.thinking = False
            return

        if code in _WATCHDOG_SIDEBAR_CODES:
            _wd_level_map: dict[str, str] = {
                M.WDST: "start",
                M.WDOK: "ok",
                M.WDWN: "warn",
                M.WDER: "error",
            }
            self._watchdog_log.add_narrative(content, _wd_level_map.get(code, "ok"))
            return

        module = event.label or ""

        if code == M.WSTG:
            self._header.update_stage(content, "running")
            ref = content.lower()
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

        if code == M.VRUN:
            self._header.update_stage("validate", "running")
            if module:
                self._header.mark_module_done("implement", module)
                self._header.count_stage_done(f"implement:{module}")

        elif code == M.VFAL:
            self._header.update_stage("validate", "failed")

        elif code == M.RSTR:
            self._header.update_stage("repair", "running")

        elif code == M.RSKP:
            if module:
                self._header.count_stage_done(f"repair:{module}")
            else:
                self._header.count_stage_done("integration_repair")

        elif code == M.RJMP:
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
            self._header.update_stage("repair", "running")
            self._sb1.stage_name = "quality \u2192 repair"


        elif code == M.VPAS:
            self._header.update_stage("validate", "success")
            if module:
                self._header.mark_module_done("validate", module)
                self._header.mark_module_done("repair", module)
                self._header.count_stage_done(f"validate:{module}")
                self._header.count_stage_done(f"repair:{module}")
            else:
                self._header.count_stage_done("integration_validate")
                self._header.count_stage_done("integration_repair")

        elif code == M.QPAS:
            self._header.update_stage("quality", "success")
            self._header.count_stage_done("quality")

        elif code == M.RVST:
            self._header.update_stage("review", "running")
        elif code == M.RVFL:
            self._header.update_stage("review", "failed")
        elif code == M.RVPS:
            self._header.update_stage("review", "success")
            self._header.count_stage_done("review")

        elif code == M.LSTR:
            self._sb1.stage_name = "loop"

        elif code == M.LITR:
            self._sb1.stage_name = f"loop {content}"

        elif code == M.LEND:
            self._sb1.stage_name = "loop complete"

        elif code == M.LERR:
            self._sb1.stage_name = "loop error"


        if code == M.WSTR:
            if self._workflow_start_time is None:
                self._workflow_start_time = time.monotonic()
            self._workflow_ended = False
        elif code in (M.WSUC, M.WFAL, M.WTMO, M.WINT):
            self._workflow_ended = True
            self._sidebar_info.thinking = False
            self._sidebar_info.waiting = False
            self._sb1.current_tool = ""
            _terminal_stage_names: dict[str, str] = {
                M.WSUC: "completed",
                M.WFAL: "failed",
                M.WTMO: "failed",
                M.WINT: "interrupted",
            }
            stage = _terminal_stage_names.get(code, "done")
            self._sb1.stage_name = stage


        self._update_routing(code, content)

        if code in STATUS_BAR_ONLY:
            return

        self._trust5_log.write_event(event)

    def _update_routing(self, code: str, content: str) -> None:
        try:
            if code == M.MMDL:
                kv = _parse_kv(content)
                model = kv.get("model", content)
                thinking = kv.get("thinking", "")
                if thinking:
                    self._sidebar_info.model_name = f"{model} (think:{thinking})"
                else:
                    self._sidebar_info.model_name = model
            elif code == M.MPRF:
                kv = _parse_kv(content)
                self._sidebar_info.provider = kv.get("provider", content)
            elif code == M.MTKN:
                kv = _parse_kv(content)
                tok_in = int(kv.get("in", "0"))
                tok_out = int(kv.get("out", "0"))
                self._sidebar_info.token_info = f"{_format_count(tok_in)} in / {_format_count(tok_out)} out"
            elif code == M.MCTX:
                kv = _parse_kv(content)
                remaining = int(kv.get("remaining", "0"))
                window = int(kv.get("window", "1"))
                pct_free = int((remaining / window) * 100) if window else 0
                self._sidebar_info.context_info = f"ctx {pct_free}% free"
            elif code == M.FCHG:
                kv = _parse_kv(content)
                path = kv.get("path", "")
                if path:
                    self._changed_files.add(path)
                    self._sidebar_info.files_changed = len(self._changed_files)
            elif code == M.SPRG:
                kv = _parse_kv(content)
                self._header.stage_total = int(kv.get("total", "0"))
                modules = int(kv.get("modules", "0"))
                if modules > 0:
                    self._header.module_count = modules
            elif code == M.WSTG:
                self._sb1.stage_name = content
                self._sb1.current_tool = ""
            elif code == M.ATRN:
                m = re.search(r"Turn \d+/\d+", content)
                self._sidebar_info.turn_info = m.group(0) if m else content
                self._sb1.current_tool = ""
                self._sidebar_info.waiting = True
            elif code == M.CTLC:
                self._sidebar_info.waiting = False
                display = content
                if "] " in display:
                    display = display.split("] ", 1)[1]
                self._sb1.current_tool = display[:60]
            elif code in _TOOL_DISPLAY_NAMES:
                self._sb1.current_tool = _TOOL_DISPLAY_NAMES[code]
            elif code == M.ASUM:
                self._sidebar_info.waiting = False
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
