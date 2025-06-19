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

    def _flush_block(self) -> None:
        content = "\n".join(self._block_buffer)
        if len(self._block_buffer) >= self.MAX_BLOCK_LINES:
            content += f"\n... [{self.MAX_BLOCK_LINES}+ lines truncated]"
        code = self._block_code

        if code == self._last_block_code and content == self._last_block_content:
            return
        self._last_block_code = code
        self._last_block_content = content

        theme = get_theme(code)

        # Skip system prompt and user input — internal noise, not useful to the user
        if code in (M.CSYS, M.CUSR):
            return
        renderable: Any  # Text | Panel — varies by code
        if code == M.CTLR:
            renderable = Text(content, style=C_DIM)
        elif code in (M.KCOD, M.TWRT, M.TRED, M.TEDT, M.KDIF):
            lexer = self._guess_lexer(self._block_label)
            if lexer == "text":
                lexer = self._sniff_lexer(content)
            syntax = Syntax(content, lexer, theme="monokai", line_numbers=True, word_wrap=True)
            renderable = Panel(
                syntax,
                title=f" {theme['title']} ",
                border_style=C_CHROME,
                box=ROUNDED,
            )
        elif code == M.VTST:
            # Test output: subdued — reference info, not what the user needs to focus on
            renderable = Panel(
                Text(content, style=C_MUTED),
                title=f" {theme['title']} ",
                border_style=C_DIM,
                box=ROUNDED,
            )
        elif code in (M.QRPT, M.PPLN, M.ARSP, M.ASUM):
            inner: Any
            try:
                inner = Markdown(content)
            except Exception:
                inner = Text(content)
            renderable = Panel(
                inner,
                title=f" {theme['title']} ",
                border_style=theme["color"],
                box=ROUNDED,
            )
        elif code == M.TBSH:
            renderable = Panel(
                Text.from_ansi(content),
                title=" Shell ",
                border_style=C_CHROME,
                box=ROUNDED,
            )
        else:
            renderable = Panel(
                Text(content),
                title=f" {theme['title']} ",
                border_style=C_CHROME,
                box=ROUNDED,
            )

        self.write(renderable)

    def _guess_lexer(self, label: str) -> str:
        label_lower = label.lower()
        if label_lower.endswith(".py"):
            return "python"
        if label_lower.endswith((".js", ".jsx")):
            return "javascript"
        if label_lower.endswith((".ts", ".tsx")):
            return "typescript"
        if label_lower.endswith(".go"):
            return "go"
        if label_lower.endswith(".rs"):
            return "rust"
        if label_lower.endswith(".md"):
            return "markdown"
        if label_lower.endswith(".json"):
            return "json"
        if label_lower.endswith((".yml", ".yaml")):
            return "yaml"
        if label_lower.endswith((".sh", ".bash")):
            return "bash"
        if label_lower.endswith((".html", ".htm")):
            return "html"
        if label_lower.endswith(".css"):
            return "css"
        if label_lower.endswith(".toml"):
            return "toml"
        if label_lower.endswith(".sql"):
            return "sql"
        if label_lower.endswith(".java"):
            return "java"
        if label_lower.endswith((".c", ".h")):
            return "c"
        if label_lower.endswith((".cpp", ".cc", ".hpp")):
            return "cpp"
        if label_lower.endswith(".rb"):
            return "ruby"
        if label_lower.endswith(".swift"):
            return "swift"
        if label_lower.endswith(".xml"):
            return "xml"
        if label_lower.endswith(".tf"):
            return "terraform"
        if os.path.basename(label_lower) == "dockerfile":
            return "docker"
        return "text"

    def _sniff_lexer(content: str) -> str:
        """Detect language from content when file extension is unknown."""
        head = content[:500]
        if any(kw in head for kw in ("import ", "from ", "def ", "class ", "#!/usr/bin/env python")):
            return "python"
        if any(kw in head for kw in ("package ", "func ", "import (")):
            return "go"
        if any(kw in head for kw in ("const ", "let ", "function ", "=> {", "require(")):
            return "javascript"
        if "fn " in head and ("let mut " in head or "use " in head):
            return "rust"
        if "#!/bin/bash" in head or "#!/bin/sh" in head:
            return "bash"
        return "text"

    def _is_duplicate(self, code: str, content: str) -> bool:
        key = f"{code}:{content[:200]}"
        if key in self._last_displayed:
            return True
        self._last_displayed[key] = content
        if len(self._last_displayed) > 200:
            self._last_displayed.pop(next(iter(self._last_displayed)))
        return False

    def _strip_agent_prefix(msg: str) -> tuple[str, str]:
        """Extract [agent_name] prefix from message if present.

        Returns (agent_label, cleaned_message).  Agent label is empty
        when the message has no bracket prefix.
        """
        if msg.startswith("[") and "] " in msg:
            idx = msg.index("] ")
            return msg[1:idx], msg[idx + 2 :]
        return "", msg

    def _print_atomic(self, ts: str, code: str, msg: str) -> None:
        if code in STATUS_BAR_ONLY:
            return
        if code in (M.CSYS, M.CUSR):
            return
        if self._is_duplicate(code, msg):
            return

        theme = get_theme(code)
        agent_label, display_msg = self._strip_agent_prefix(msg)
        text = Text()

        if theme.get("pill"):
            # Pill badge: colored background — for major lifecycle events
            text.append(f" {theme['marker']} ", style=f"bold {C_BG} on {theme['color']}")
            text.append("  ")
            if agent_label:
                text.append(f"{agent_label}  ", style=C_DIM)
            text.append(display_msg, style=self._msg_style(code))
        else:
            # Colored text marker — for tool calls and secondary events
            text.append(f" {theme['marker']}", style=f"bold {theme['color']}")
            text.append("  ")
            if agent_label:
                text.append(f"{agent_label}  ", style=C_DIM)
            text.append(display_msg, style=self._msg_style(code))

        self.write(text)

    def _msg_style(self, code: str) -> str:
        """Return text style based on event semantic category.

        Hierarchy: bold+color for important, color for normal, dim for noise.
        """
        # Errors — bold rose, always prominent
        if code in (M.WFAL, M.AERR, M.SERR, M.RFAL, M.QFAL, M.VFAL, M.LERR):
            return f"bold {C_RED}"
        # Success — bold green
        if code in (M.WSUC, M.VPAS, M.QPAS, M.REND, M.LEND):
            return f"bold {C_GREEN}"
        # Warnings / retries — bold copper
        if code in (M.SWRN, M.ARTY, M.WJMP, M.RJMP, M.AFBK):
            return f"bold {C_AMBER}"
        # Stage lifecycle — bold mauve
        if code == M.WSTG:
            return f"bold {C_LAVENDER}"
        # Workflow start — bold gold
        if code == M.WSTR:
            return f"bold {C_BLUE}"
        # Validation / quality running — bold mauve
        if code in (M.VRUN, M.QRUN):
            return f"bold {C_LAVENDER}"
        # Repair start — bold copper
        if code == M.RSTR:
            return f"bold {C_AMBER}"
        # Tool calls — sage (visible but subordinate)
        if code in (M.TCAL, M.CTLC, M.TBSH, M.TRED, M.TWRT, M.TEDT, M.TGLB, M.TGRP, M.TPKG, M.TINI):
            return C_TEAL
        # Tool results — dim (background noise)
        if code in (M.TRES, M.CTLR):
            return C_DIM
        # Info — bold gold
        if code == M.SINF:
            return f"bold {C_BLUE}"
        # Default — secondary (not bright, reserve cream for AI response content)
        return C_SECONDARY

class HeaderWidget(Static):
    """Pipeline progress header with module-aware stage badges.

    Tracks per-module completion for each phase so that parallel pipelines
    show accurate progress (e.g. "TEST 2/3" = 2 of 3 modules done).
    A phase badge turns green only when ALL modules have completed it.
    """
    STAGES = [('plan', 'PLAN'), ('write_tests', 'TEST'), ('implement', 'CODE'), ('validate', 'VERIFY'), ('repair', 'FIX'), ('quality', 'GATE')]
    _MODULE_PHASES: frozenset[str] = frozenset({'write_tests', 'implement', 'validate', 'repair'})
    current_stage: reactive[str] = reactive('plan')
    completed_stages: reactive[set[str]] = reactive(set)
    stage_total: reactive[int] = reactive(0)
    stages_done: reactive[int] = reactive(0)
    module_count: reactive[int] = reactive(0)
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Per-phase module tracking: {phase_key: set of module labels done}
        self._phase_modules: dict[str, set[str]] = {}
        # Dedup for stage counter: set of "phase:module" keys
        self._counted_stages: set[str] = set()

    def _stage_index(self, key: str) -> int:
        for i, (k, _) in enumerate(self.STAGES):
            if k == key:
                return i
        return -1

    def phase_done_count(self, phase_key: str) -> int:
        return len(self._phase_modules.get(phase_key, set()))

    def mark_module_done(self, phase_key: str, module: str) -> None:
        """Record that a module completed this phase."""
        if phase_key not in self._phase_modules:
            self._phase_modules[phase_key] = set()
        self._phase_modules[phase_key].add(module)
        # Auto-complete the phase when all modules are done.
        mc = self.module_count
        if mc > 0 and len(self._phase_modules[phase_key]) >= mc:
            self.completed_stages = self.completed_stages | {phase_key}
        self.refresh()

    def count_stage_done(self, stage_id: str) -> None:
        """Increment the progress counter (deduped by stage_id)."""
        if stage_id not in self._counted_stages:
            self._counted_stages.add(stage_id)
            self.stages_done = len(self._counted_stages)

    def update_stage(
        self,
        stage_ref: str,
        status: str = "running",
    ) -> None:
        ref = stage_ref.lower()
        key = self._match_stage_key(ref)
        if key is None:
            return

        new_idx = self._stage_index(key)
        cur_idx = self._stage_index(self.current_stage)

        if status == "success":
            self.completed_stages = self.completed_stages | {key}
        elif status == "failed":
            pass  # Don't mark complete; just update current_stage below
        elif status == "running":
            # A new stage started — mark all preceding stages as done.
            preceding: set[str] = set()
            for k, _ in self.STAGES:
                if k == key:
                    break
                preceding.add(k)
            if preceding - self.completed_stages:
                self.completed_stages = self.completed_stages | preceding

        # Only advance the active indicator forward, never backward.
        # In parallel pipelines, later modules may start earlier stages
        # (e.g. test-writer for module 2) while the header has already
        # moved past them (e.g. validate for module 1).
        #
        # Exception: validate and repair cycle during the repair loop.
        # Allow backward transition from repair to validate so the
        # header reflects the active phase.
        allow_cycle = self.current_stage == "repair" and key == "validate"
        if new_idx >= cur_idx or allow_cycle:
            self.current_stage = key

    def _match_stage_key(ref: str) -> str | None:
        """Match event content to a header stage key."""
        if "test-writer" in ref or "test_writer" in ref or "write test" in ref:
            return "write_tests"
        if "implement" in ref:
            return "implement"
        if "validate" in ref or "run tests" in ref:
            return "validate"
        if "repair" in ref or "fix failure" in ref:
            return "repair"
        if "quality" in ref or "trust 5" in ref:
            return "quality"
        if "plan" in ref or "planner" in ref:
            return "plan"
        return None

    def render(self) -> Text:
        text = Text()
        text.append(" TRUST5 ", style=f"bold {C_BG} on {C_BLUE}")

        # Progress counter: actual completed stages / total
        if self.stage_total > 0:
            text.append(f"  {self.stages_done}/{self.stage_total}  ", style=C_MUTED)
        else:
            text.append("  ", style=C_MUTED)

        mc = self.module_count
        for i, (key, label) in enumerate(self.STAGES):
            if i > 0:
                text.append(" \u2500 ", style=C_CHROME)

            done = self.phase_done_count(key)
            show_frac = mc > 1 and key in self._MODULE_PHASES

            if key in self.completed_stages:
                text.append(f" \u2713 {label} ", style=f"bold {C_GREEN}")
            elif key == self.current_stage:
                if show_frac:
                    text.append(f" {label} {done}/{mc} ", style=f"bold {C_BG} on {C_LAVENDER}")
                else:
                    text.append(f" {label} ", style=f"bold {C_BG} on {C_LAVENDER}")
            elif show_frac and done > 0:
                # Not active, but some modules completed — show progress
                text.append(f" {label} {done}/{mc} ", style=C_SECONDARY)
            else:
                text.append(f" {label} ", style=C_MUTED)

        return text

class StatusBar1(Static):
    """Upper status bar: stage, elapsed, turn, tool, files, thinking/waiting."""
    stage_name: reactive[str] = reactive('')
    elapsed: reactive[str] = reactive('')
    turn_info: reactive[str] = reactive('')
    current_tool: reactive[str] = reactive('')
    files_changed: reactive[int] = reactive(0)
    thinking: reactive[bool] = reactive(False)
    waiting: reactive[bool] = reactive(False)
    _SPINNER_FRAMES = ('⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏')
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._spinner_tick = 0
        self._spinner_timer: Any = None

    def render(self) -> Text:
        text = Text()
        if self.stage_name:
            text.append(f" {self.stage_name}", style=f"bold {C_LAVENDER}")
        else:
            text.append(" idle", style=C_DIM)

        if self.elapsed:
            text.append(f"  {self.elapsed}", style=C_MUTED)
        if self.turn_info:
            text.append(f"  {self.turn_info}", style=C_MUTED)
        if self.thinking:
            frame = self._SPINNER_FRAMES[self._spinner_tick % len(self._SPINNER_FRAMES)]
            text.append(f"  {frame} thinking", style=f"bold {C_AMBER}")
        elif self.waiting:
            frame = self._SPINNER_FRAMES[self._spinner_tick % len(self._SPINNER_FRAMES)]
            text.append(f"  {frame} generating", style=C_MUTED)
        if self.current_tool:
            text.append(f"  \u25b8 {self.current_tool}", style=f"bold {C_TEAL}")
        if self.files_changed > 0:
            text.append(f"  {self.files_changed} file(s)", style=f"bold {C_GREEN}")
        return text

    def _needs_spinner(self) -> bool:
        return self.thinking or self.waiting

    def _start_spinner(self) -> None:
        if self._spinner_timer is None:
            self._spinner_tick = 0
            self._spinner_timer = self.set_interval(0.08, self._animate_spinner)

    def _stop_spinner_if_idle(self) -> None:
        if not self._needs_spinner() and self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
            self.refresh()

    def watch_thinking(self, value: bool) -> None:
        """Start/stop spinner for thinking state."""
        if value:
            self._start_spinner()
        else:
            self._stop_spinner_if_idle()

    def watch_waiting(self, value: bool) -> None:
        """Start/stop spinner for waiting-for-LLM state."""
        if value:
            self._start_spinner()
        else:
            self._stop_spinner_if_idle()

    def _animate_spinner(self) -> None:
        if self._needs_spinner():
            self._spinner_tick += 1
            self.refresh()

class StatusBar0(Static):
    """Lower status bar: model, tokens, context, keybindings."""
    model_name: reactive[str] = reactive('')
    provider: reactive[str] = reactive('')
    token_info: reactive[str] = reactive('')
    context_info: reactive[str] = reactive('')
