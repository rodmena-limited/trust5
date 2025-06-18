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
