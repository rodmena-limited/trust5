import os
from typing import Any
from rich.box import ROUNDED
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from textual.reactive import reactive
from textual.widgets import RichLog, Static
from ..core.event_bus import (
    K_BLOCK_END,
    K_BLOCK_LINE,
    K_BLOCK_START,
    K_MSG,
    Event,
)
from ..core.message import M
C_BG = "#0c0a08"  # Near-black with warm brown undertone
C_SURFACE = "#151210"  # Dark chocolate surface
C_BORDER = "#2a2420"  # Warm dark border
C_CHROME = "#3a322c"  # Warm separator
C_TEXT = "#e8ddd0"  # Cream white — warm primary text
C_SECONDARY = "#b0a898"  # Warm taupe — normal messages
C_MUTED = "#706860"  # Warm grey — timestamps, noise
C_DIM = "#483f38"  # Dark warm grey — decorative, faint
C_BLUE = "#d4a054"  # Warm gold — primary accent, headers, brand
C_TEAL = "#7ab08a"  # Sage green — tool operations
C_GREEN = "#8cc084"  # Warm green — success
C_AMBER = "#d4943c"  # Copper — thinking, warnings, retries
C_RED = "#c87070"  # Dusty rose — errors, failures
C_LAVENDER = "#b08cb8"  # Dusty mauve — stages, validation
C_DIM_TEAL = "#5a8068"
C_DIM_GREEN = "#68986c"
C_DIM_AMBER = "#a07838"
C_DIM_RED = "#985858"
C_DIM_LAVENDER = "#887098"
THEME: dict[str, dict[str, Any]] = {
    # Agent
    M.ATHK: {"marker": " .. ", "color": C_AMBER, "title": "Thinking", "pill": False},
    M.ARSP: {"marker": " >> ", "color": C_GREEN, "title": "Response", "pill": False},
    M.ASUM: {"marker": " >> ", "color": C_GREEN, "title": "Summary", "pill": False},
    M.AERR: {"marker": " !! ", "color": C_RED, "title": "Error", "pill": True},
    M.ARTY: {"marker": " <> ", "color": C_AMBER, "title": "Retry", "pill": True},
    M.AFBK: {"marker": " <> ", "color": C_AMBER, "title": "Fallback", "pill": True},
    # Context
    M.CSYS: {"marker": " SYS", "color": C_DIM, "title": "System", "pill": False},
    M.CUSR: {"marker": " IN ", "color": C_BLUE, "title": "Input", "pill": False},
    M.CAST: {"marker": " AI ", "color": C_SECONDARY, "title": "Assistant", "pill": False},
    M.CTLC: {"marker": "  > ", "color": C_TEAL, "title": "Call", "pill": False},
    M.CTLR: {"marker": "  < ", "color": C_DIM, "title": "Result", "pill": False},
    # Tools
    M.TCAL: {"marker": "  > ", "color": C_TEAL, "title": "Tool", "pill": False},
    M.TRES: {"marker": "  < ", "color": C_DIM, "title": "Result", "pill": False},
    M.TBSH: {"marker": "  $ ", "color": C_TEAL, "title": "Shell", "pill": False},
    M.TWRT: {"marker": "  W ", "color": C_GREEN, "title": "Write", "pill": False},
    M.TRED: {"marker": "  R ", "color": C_BLUE, "title": "Read", "pill": False},
    M.TEDT: {"marker": "  E ", "color": C_GREEN, "title": "Edit", "pill": False},
    M.TGLB: {"marker": "  G ", "color": C_DIM_TEAL, "title": "Glob", "pill": False},
    M.TGRP: {"marker": "  S ", "color": C_DIM_TEAL, "title": "Search", "pill": False},
    M.TPKG: {"marker": " PKG", "color": C_TEAL, "title": "Package", "pill": False},
    M.TINI: {"marker": "INIT", "color": C_TEAL, "title": "Init", "pill": False},
    # Workflow
    M.WSTR: {"marker": " >> ", "color": C_BLUE, "title": "Workflow", "pill": True},
    M.WSUC: {"marker": " OK ", "color": C_GREEN, "title": "Success", "pill": True},
    M.WFAL: {"marker": "FAIL", "color": C_RED, "title": "Failed", "pill": True},
    M.WTMO: {"marker": " TO ", "color": C_RED, "title": "Timeout", "pill": True},
    M.WRCV: {"marker": " RC ", "color": C_AMBER, "title": "Recovered", "pill": True},
    M.WSTG: {"marker": " >> ", "color": C_LAVENDER, "title": "Stage", "pill": True},
    M.WJMP: {"marker": " JMP", "color": C_AMBER, "title": "Jump", "pill": True},
    M.WSKP: {"marker": "SKIP", "color": C_MUTED, "title": "Skipped", "pill": True},
    M.WINT: {"marker": " INT", "color": C_RED, "title": "Interrupted", "pill": True},
    # Validation
    M.VRUN: {"marker": "TEST", "color": C_LAVENDER, "title": "Validation", "pill": True},
    M.VPAS: {"marker": "PASS", "color": C_GREEN, "title": "Tests Passed", "pill": True},
    M.VFAL: {"marker": "FAIL", "color": C_RED, "title": "Tests Failed", "pill": True},
    M.VTST: {"marker": "TEST", "color": C_DIM_LAVENDER, "title": "Test Output", "pill": False},
    # Repair
    M.RSTR: {"marker": " FIX", "color": C_AMBER, "title": "Repair", "pill": True},
    M.REND: {"marker": " FIX", "color": C_GREEN, "title": "Repair Done", "pill": True},
    M.RFAL: {"marker": " FIX", "color": C_RED, "title": "Repair Failed", "pill": True},
    M.RJMP: {"marker": " JMP", "color": C_AMBER, "title": "Repair Jump", "pill": True},
    M.RSKP: {"marker": "SKIP", "color": C_MUTED, "title": "Repair Skipped", "pill": True},
    # Quality
    M.QRUN: {"marker": "GATE", "color": C_LAVENDER, "title": "Quality", "pill": True},
    M.QPAS: {"marker": "GATE", "color": C_GREEN, "title": "Quality OK", "pill": True},
    M.QFAL: {"marker": "GATE", "color": C_RED, "title": "Quality Failed", "pill": True},
    M.QJMP: {"marker": " JMP", "color": C_AMBER, "title": "Quality Jump", "pill": True},
    M.QVAL: {"marker": "  QA", "color": C_DIM_LAVENDER, "title": "Validation", "pill": False},
    M.QRPT: {"marker": " QA ", "color": C_AMBER, "title": "Quality Report", "pill": False},
    # System
    M.SINF: {"marker": "  i ", "color": C_BLUE, "title": "Info", "pill": True},
    M.SWRN: {"marker": "  ! ", "color": C_AMBER, "title": "Warning", "pill": True},
    M.SERR: {"marker": " !! ", "color": C_RED, "title": "Error", "pill": True},
    # Artifacts
    M.KDIF: {"marker": "DIFF", "color": C_TEAL, "title": "Diff", "pill": False},
    M.KCOD: {"marker": "CODE", "color": C_TEAL, "title": "Code", "pill": False},
    M.PPLN: {"marker": "PLAN", "color": C_BLUE, "title": "Plan", "pill": True},
    # Loop
    M.LSTR: {"marker": "LOOP", "color": C_LAVENDER, "title": "Loop", "pill": True},
    M.LEND: {"marker": "LOOP", "color": C_GREEN, "title": "Loop Done", "pill": True},
    M.LFIX: {"marker": " FIX", "color": C_TEAL, "title": "Fix", "pill": False},
    M.LERR: {"marker": " !! ", "color": C_RED, "title": "Loop Error", "pill": True},
    M.LITR: {"marker": "LOOP", "color": C_DIM_LAVENDER, "title": "Iteration", "pill": False},
    M.LDIG: {"marker": " DX ", "color": C_DIM_LAVENDER, "title": "Diagnostics", "pill": False},
    # Blocked
    M.UASK: {"marker": "  ?", "color": C_AMBER, "title": "Question", "pill": True},
    M.UAUT: {"marker": "AUTO", "color": C_AMBER, "title": "Auto-Answer", "pill": True},
}
_DEFAULT_THEME: dict[str, Any] = {"marker": "  . ", "color": C_SECONDARY, "title": "", "pill": False}
STATUS_BAR_ONLY: frozenset[str] = frozenset(
    {
        M.MMDL,
        M.MPRF,
        M.MTKN,
        M.MCTX,
        M.MBDG,
        M.SELP,
        M.SPRG,
        M.FCHG,
        M.GSTS,
        M.CREQ,
        M.CRES,
        M.CTKN,
        M.CMDL,
        M.ATRN,
        M.SDBG,
        M.SDB,
        M.SCFG,
        M.PPRG,
        M.TRES,
        M.CTLR,  # Tool internals — result size & LLM routing
    }
)

def get_theme(code: str) -> dict[str, Any]:
    return THEME.get(code, _DEFAULT_THEME)

def _parse_kv(raw: str) -> dict[str, str]:
    """Parse 'key=value key2=value2' into a dict."""
    result: dict[str, str] = {}
    for part in raw.split():
        if "=" in part:
            k, v = part.split("=", 1)
            result[k] = v
    return result

def _format_count(n: int) -> str:
    """Format large numbers concisely: 1234 -> '1.2K', 1234567 -> '1.2M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)

class Trust5Log(RichLog):
    """Scrollable log with block accumulation, syntax highlighting, and markdown.

    Auto-scroll: follows new content UNLESS the user scrolls up (mouse wheel).
    Detection uses MouseScrollUp/Down events (only fired by user input, never
    by programmatic scroll_end). scroll_end() is overridden as a guard so that
    even deferred scrolls scheduled before the user scrolled are suppressed.
    """
    MAX_BLOCK_LINES = 500
    MAX_THINKING_LINES = 50
    _SCROLL_BOTTOM_THRESHOLD = 3
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Disable Textual's auto_scroll — we handle it ourselves
        self.auto_scroll = False
        self._block_buffer: list[str] = []
        self._block_label: str = ""
        self._block_code: str = ""
        self._in_block = False
        self._stream_code: str = ""
        self._stream_label: str = ""
        self._thinking_content: str = ""
        self._thinking_line_buffer: str = ""
        self._response_content: str = ""
        self._response_line_buffer: str = ""
        self._user_scrolled = False

        # Deduplication tracking
        self._last_displayed: dict[str, str] = {}
        self._last_block_content: str = ""
        self._last_block_code: str = ""

    def _is_at_bottom(self) -> bool:
        return self.scroll_offset.y >= (self.virtual_size.height - self.size.height - self._SCROLL_BOTTOM_THRESHOLD)

    def scroll_end(self, *args: Any, **kwargs: Any) -> None:
        """Guard: suppress ALL scroll-to-end calls while user has scrolled up.

        This catches both direct calls from write() AND deferred calls
        scheduled by Textual's internal call_after_refresh().
        """
        if self._user_scrolled:
            return
        super().scroll_end(*args, **kwargs)

    def write(self, *args: Any, **kwargs: Any) -> Any:
        """Write content, then scroll to bottom only if user hasn't scrolled up."""
        result = super().write(*args, **kwargs)
        if not self._user_scrolled:
            self.scroll_end(animate=False)
        return result

    def on_mouse_scroll_up(self, event: Any) -> None:
        """User scrolled up with mouse wheel — pause auto-scroll."""
        self._user_scrolled = True

    def on_mouse_scroll_down(self, event: Any) -> None:
        """User scrolled down — resume auto-scroll if they reached the bottom."""
        self.call_later(self._check_resume_scroll)

    def _check_resume_scroll(self) -> None:
        """Resume auto-scroll if the user has scrolled back to the bottom."""
        if self._user_scrolled and self._is_at_bottom():
            self._user_scrolled = False

    def write_event(self, event: Event) -> None:
        kind = event.kind
        code = event.code
        msg = event.msg
        ts = event.ts

        if kind == K_BLOCK_START:
            self._in_block = True
            self._block_code = code
            self._block_label = event.label or ""
            self._block_buffer = []
            return

        if kind == K_BLOCK_LINE:
            if self._in_block and len(self._block_buffer) < self.MAX_BLOCK_LINES:
                self._block_buffer.append(msg)
            return

        if kind == K_BLOCK_END:
            if self._in_block:
                self._flush_block()
            self._in_block = False
            return

        if kind == K_MSG:
            self._print_atomic(ts, code, msg)

    def write_stream_start(self, code: str, label: str) -> None:
        self._stream_code = code
        self._stream_label = label
        if code == M.ATHK:
            # Thinking: suppress content, just track that it's happening
            self._thinking_content = ""
            self._thinking_line_buffer = ""
        else:
            self._response_content = ""
            self._response_line_buffer = ""

    def write_stream_token(self, token: str, code: str = "") -> None:
        if not token:
            return

        effective_code = code or self._stream_code or M.ARSP
        if effective_code == M.ATHK:
            # Silently accumulate thinking — don't render to log
            self._thinking_content += token
        else:
            self._response_content += token
            self._response_line_buffer += token
            while "\n" in self._response_line_buffer:
                line, self._response_line_buffer = self._response_line_buffer.split("\n", 1)
                if line.strip():
                    self.write(Text(f"    {line}", style=C_TEXT))

    def write_stream_end(self) -> None:
        if self._stream_code == M.ATHK:
            self._thinking_content = ""
            self._thinking_line_buffer = ""
        elif self._response_content:
            if self._response_line_buffer.strip():
                self.write(Text(f"    {self._response_line_buffer}", style=C_TEXT))
            self._response_line_buffer = ""
            self.write(Text("  " + "\u2500" * 50, style=C_DIM))
            self._response_content = ""

        self._stream_code = ""
        self._stream_label = ""
