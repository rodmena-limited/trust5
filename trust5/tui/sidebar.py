"""Right sidebar: logo, model info, runtime stats, and watchdog narratives."""

from __future__ import annotations

import re
from typing import Any

from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import Markdown
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import RichLog, Static

from .theme import (
    C_AMBER,
    C_DIM,
    C_DIM_PURPLE,
    C_GREEN,
    C_MUTED,
    C_PURPLE,
    C_RED,
    C_SECONDARY,
    C_TEXT,
)

# ─── Logo ────────────────────────────────────────────────────────────────────

_LOGO_TRUST = [
    "▀█▀ █▀▄ █ █ █▀▀ ▀█▀",
    " █  █▀▄ █ █ ▀▀█  █ ",
    " ▀  ▀ ▀ ▀▀▀ ▀▀▀  ▀ ",
]
_LOGO_5 = [
    " █▀▀",
    " ▀▀█",
    " ▀▀▀",
]


class SidebarLogo(Static):
    """Big TRUST5 ASCII art logo — TRUST in green, 5 in white."""

    def render(self) -> Text:
        t = Text(justify="center")
        for trust_line, five_line in zip(_LOGO_TRUST, _LOGO_5):
            t.append(trust_line, style=f"bold {C_GREEN}")
            t.append(five_line, style="bold #ffffff")
            t.append("\n")
        t.append("autonomous coding agent", style=C_DIM)
        return t


# ─── Model + Runtime Info ────────────────────────────────────────────────────

_SPINNER_FRAMES = (
    "\u280b",
    "\u2819",
    "\u2839",
    "\u2838",
    "\u283c",
    "\u2834",
    "\u2826",
    "\u2827",
    "\u2807",
    "\u280f",
)


class SidebarInfo(Static):
    """Model, provider, tokens, runtime stats, and keyboard shortcuts."""

    model_name: reactive[str] = reactive("")
    provider: reactive[str] = reactive("")
    token_info: reactive[str] = reactive("")
    context_info: reactive[str] = reactive("")
    elapsed: reactive[str] = reactive("")
    turn_info: reactive[str] = reactive("")
    thinking: reactive[bool] = reactive(False)
    waiting: reactive[bool] = reactive(False)
    files_changed: reactive[int] = reactive(0)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._spinner_tick = 0
        self._spinner_timer: Any = None

    def render(self) -> Text:
        t = Text()

        if self.model_name:
            t.append("model ", style=C_DIM)
            t.append(f"{self.model_name}\n", style=f"bold {C_GREEN}")
        if self.provider:
            t.append("via   ", style=C_DIM)
            t.append(f"{self.provider}\n", style=C_SECONDARY)
        if self.token_info:
            t.append("tok   ", style=C_DIM)
            t.append(f"{self.token_info}\n", style=C_SECONDARY)
        if self.context_info:
            t.append("ctx   ", style=C_DIM)
            t.append(f"{self.context_info}\n", style=C_AMBER)

        has_model = bool(self.model_name or self.provider)
        has_runtime = bool(self.elapsed or self.turn_info or self.thinking or self.waiting)

        if has_model and has_runtime:
            t.append("\n")

        if self.elapsed:
            t.append("time  ", style=C_DIM)
            t.append(f"{self.elapsed}\n", style=C_SECONDARY)
        if self.turn_info:
            t.append("turn  ", style=C_DIM)
            t.append(f"{self.turn_info}\n", style=C_SECONDARY)

        if self.thinking:
            frame = _SPINNER_FRAMES[self._spinner_tick % len(_SPINNER_FRAMES)]
            t.append("thinking ", style=f"bold {C_AMBER}")
            t.append(f"{frame}\n", style=f"bold {C_AMBER}")
        elif self.waiting:
            frame = _SPINNER_FRAMES[self._spinner_tick % len(_SPINNER_FRAMES)]
            t.append("generating ", style=C_MUTED)
            t.append(f"{frame}\n", style=C_MUTED)

        if self.files_changed > 0:
            t.append("files ", style=C_DIM)
            t.append(f"{self.files_changed} changed\n", style=f"bold {C_GREEN}")

        if not t.plain.strip():
            t.append("waiting...\n", style=C_DIM)

        return t

    def _start_spinner(self) -> None:
        if self._spinner_timer is None:
            self._spinner_tick = 0
            self._spinner_timer = self.set_interval(0.08, self._animate_spinner)

    def _stop_spinner_if_idle(self) -> None:
        if not (self.thinking or self.waiting) and self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
            self.refresh()

    def watch_thinking(self, value: bool) -> None:
        if value:
            self._start_spinner()
        else:
            self._stop_spinner_if_idle()

    def watch_waiting(self, value: bool) -> None:
        if value:
            self._start_spinner()
        else:
            self._stop_spinner_if_idle()

    def _animate_spinner(self) -> None:
        if self.thinking or self.waiting:
            self._spinner_tick += 1
            self.refresh()


# ─── Watchdog ────────────────────────────────────────────────────────────────


_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_RULE_RE = re.compile(r"^([-*_])\1{2,}\s*$", re.MULTILINE)


def _strip_md_chrome(text: str) -> str:
    """Convert markdown headings to bold and remove horizontal rules.

    Rich renders ``# heading`` as a bordered Panel which looks heavy
    in a narrow sidebar.  Convert to ``**heading**`` instead.
    """
    # Convert headings: '## Foo' → '**Foo**'
    text = _HEADING_RE.sub(lambda m: "**", text)
    # Close bold at end of that line
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("**") and not line.endswith("**"):
            line = line.rstrip() + "**"
        lines.append(line)
    text = "\n".join(lines)
    # Remove horizontal rules
    text = _RULE_RE.sub("", text)
    return text.strip()


class _WidthAwareMarkdown(Markdown):
    """Markdown that renders at a specific width to prevent clipping."""

    def __init__(self, markup: str, width: int = 40) -> None:
        super().__init__(markup)
        self._render_width = width

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        clamped = options.update_width(min(self._render_width, options.max_width))
        yield from super().__rich_console__(console, clamped)


class WatchdogHeader(Static):
    """Blinking watchdog label — circle pulses to show active monitoring."""

    _CIRCLE_STYLES = (
        f"bold {C_PURPLE}",
        f"{C_DIM_PURPLE}",
        f"bold {C_PURPLE}",
        f"bold {C_PURPLE}",
        f"bold {C_PURPLE}",
        f"bold {C_PURPLE}",
    )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._blink_tick = 0
        self._blink_timer: Any = None

    def on_mount(self) -> None:
        self._blink_timer = self.set_interval(0.6, self._animate_blink)

    def _animate_blink(self) -> None:
        self._blink_tick += 1
        self.refresh()

    def render(self) -> Text:
        t = Text()
        idx = self._blink_tick % len(self._CIRCLE_STYLES)
        t.append("\u25c9 ", style=self._CIRCLE_STYLES[idx])
        t.append("WATCHDOG", style=f"bold {C_PURPLE}")
        return t


class WatchdogLog(RichLog):
    """Scrollable watchdog narrative area in the sidebar."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.auto_scroll = True
        self._last_narrative: str = ""

    def _available_width(self) -> int:
        try:
            return max(self.size.width - 6, 20)
        except (AttributeError, ValueError):  # widget not yet mounted or zero-size
            return 32

    def add_narrative(self, content: str, level: str = "ok") -> None:
        """Add a watchdog narrative entry.
        Args:
            content: Narrative text (may contain markdown).
            level: One of 'ok', 'warn', 'error', 'start'.
        """
        if content == self._last_narrative:
            return
        self._last_narrative = content
        w = self._available_width()

        if len(content) > 40:
            cleaned = _strip_md_chrome(content)
            self.write(_WidthAwareMarkdown(cleaned, width=w))
        else:
            color_map: dict[str, str] = {
                "ok": C_DIM_PURPLE,
                "warn": C_PURPLE,
                "error": C_RED,
                "start": C_PURPLE,
            }
            color = color_map.get(level, C_DIM_PURPLE)
            t = Text()
            t.append("\u25c9 ", style=f"bold {color}")
            t.append(content, style=C_TEXT)
            self.write(t)


# ─── Sidebar Container ──────────────────────────────────────────────────────


class Sidebar(Vertical):
    """Right sidebar containing logo, model info, and watchdog feed."""

    def compose(self) -> ComposeResult:
        yield SidebarLogo(id="sidebar-logo")
        yield SidebarInfo(id="sidebar-info")
        yield WatchdogHeader(id="watchdog-header")
        yield WatchdogLog(
            id="watchdog-log",
            markup=False,
            max_lines=200,
            wrap=True,
        )
