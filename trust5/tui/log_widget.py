import os
from typing import Any

from rich.box import ROUNDED
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from textual.widgets import RichLog

from ..core.constants import TUI_MAX_BLOCK_LINES, TUI_MAX_THINKING_LINES
from ..core.event_bus import (
    K_BLOCK_END,
    K_BLOCK_LINE,
    K_BLOCK_START,
    K_MSG,
    Event,
)
from ..core.message import M
from .theme import (
    C_AMBER,
    C_BG,
    C_BLUE,
    C_CHROME,
    C_CYAN,
    C_DIM,
    C_DIM_PURPLE,
    C_GREEN,
    C_LAVENDER,
    C_MUTED,
    C_PURPLE,
    C_RED,
    C_SECONDARY,
    C_TEAL,
    C_TEXT,
    STATUS_BAR_ONLY,
    get_theme,
)

# ─── Helpers ─────────────────────────────────────────────────────────────────


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


# ─── Trust5Log Widget ────────────────────────────────────────────────────────


class Trust5Log(RichLog):
    """Scrollable log with block accumulation, syntax highlighting, and markdown.

    Auto-scroll: follows new content UNLESS the user scrolls up (mouse wheel).
    Detection uses MouseScrollUp/Down events (only fired by user input, never
    by programmatic scroll_end). scroll_end() is overridden as a guard so that
    even deferred scrolls scheduled before the user scrolled are suppressed.
    """

    MAX_BLOCK_LINES = TUI_MAX_BLOCK_LINES
    MAX_THINKING_LINES = TUI_MAX_THINKING_LINES


    _PANEL_MAX_WIDTH = 116
    _SCROLL_BOTTOM_THRESHOLD = 3

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Disable Textual's auto_scroll — we handle it ourselves
        self.auto_scroll = False
        self._block_buffer: list[str] = []
        self._in_json_comment = False
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
        """Write content. Scroll is deferred to the batch dispatcher in app.py."""
        return super().write(*args, **kwargs)

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

    # ── Streaming ─────────────────────────────────────────────────────────

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
                # Filter out <!-- REVIEW_FINDINGS JSON ... --> blocks from streaming
                if "<!-- REVIEW_FINDINGS" in line:
                    self._in_json_comment = True
                    continue
                if self._in_json_comment:
                    if "-->" in line:
                        self._in_json_comment = False
                    continue
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
            self.write(Text("  " + "\u2500" * 50, style=C_CHROME))
            self._response_content = ""

        self._stream_code = ""
        self._stream_label = ""

    # ── Block rendering ───────────────────────────────────────────────────

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
                width=self._PANEL_MAX_WIDTH,
            )
        elif code == M.VTST:
            # Test output: subdued — reference info, not what the user needs to focus on
            renderable = Panel(
                Text(content, style=C_MUTED),
                title=f" {theme['title']} ",
                border_style=C_DIM,
                box=ROUNDED,
                width=self._PANEL_MAX_WIDTH,
            )
        elif code == M.RVRP:
            # Review report: LLM-generated content — render as Markdown
            renderable = Panel(
                Markdown(content),
                title=f" {theme['title']} ",
                border_style=theme["color"],
                box=ROUNDED,
                width=self._PANEL_MAX_WIDTH,
            )
        elif code == M.QRPT:
            # Quality report: plain text, not Markdown — contains lint output
            # with carets and line numbers that Markdown collapses into paragraphs.
            renderable = Panel(
                Text(content, style=C_TEXT),
                title=f" {theme['title']} ",
                border_style=theme["color"],
                box=ROUNDED,
                width=self._PANEL_MAX_WIDTH,
            )
        elif code == M.TBSH:
            renderable = Panel(
                Text.from_ansi(content),
                title=" Shell ",
                border_style=C_CHROME,
                box=ROUNDED,
                width=self._PANEL_MAX_WIDTH,
            )
        elif code in (M.WDWN, M.WDER):
            border = C_RED if code == M.WDER else C_PURPLE
            renderable = Panel(
                Markdown(content),
                title=f" {theme['title']} ",
                border_style=border,
                box=ROUNDED,
                width=self._PANEL_MAX_WIDTH,
            )
        else:
            renderable = Panel(
                Text(content),
                title=f" {theme['title']} ",
                border_style=C_CHROME,
                box=ROUNDED,
                width=self._PANEL_MAX_WIDTH,
            )

        self.write(Text(""))  # spacer before panel
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

    @staticmethod
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

    # ── Atomic messages ───────────────────────────────────────────────────

    def _is_duplicate(self, code: str, content: str) -> bool:
        key = f"{code}:{content[:200]}"
        if key in self._last_displayed:
            return True
        self._last_displayed[key] = content
        if len(self._last_displayed) > 200:
            self._last_displayed.pop(next(iter(self._last_displayed)))
        return False

    @staticmethod
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
        # Tool calls — each type gets its own color for scanability
        if code == M.TBSH:
            return C_AMBER  # Shell — copper (stands out as side-effect)
        if code in (M.TRED, M.TGLB):
            return C_CYAN  # File reads / glob — cyan (read-only, informational)
        if code in (M.TWRT, M.TEDT):
            return C_GREEN  # Writes / edits — green (mutation)
        if code == M.TGRP:
            return C_BLUE  # Search — gold (discovery)
        if code in (M.TCAL, M.CTLC, M.TPKG, M.TINI):
            return C_TEAL  # Generic tools — sage
        # Tool results — dim (background noise)
        if code in (M.TRES, M.CTLR):
            return C_DIM
        # Info — bold gold
        if code == M.SINF:
            return f"bold {C_BLUE}"
        # Watchdog — purple for warnings, red for errors, purple for lifecycle
        if code in (M.WDWN, M.WDER):
            return f"bold {C_RED}" if code == M.WDER else f"bold {C_PURPLE}"
        if code in (M.WDST, M.WDOK):
            return C_PURPLE if code == M.WDST else C_DIM_PURPLE
        # Default — secondary (not bright, reserve cream for AI response content)
        return C_SECONDARY
