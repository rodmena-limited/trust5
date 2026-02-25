"""Right sidebar: logo, model info, and watchdog narratives."""

from __future__ import annotations

from typing import Any

from rich.box import ROUNDED
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import RichLog, Static

from .theme import (
    C_AMBER,
    C_BLUE,
    C_CHROME,
    C_DIM,
    C_DIM_PURPLE,
    C_PURPLE,
    C_RED,
    C_SECONDARY,
)

# ─── Logo ────────────────────────────────────────────────────────────────────


class SidebarLogo(Static):
    """Compact TRUST5 logo for sidebar header."""

    def render(self) -> Panel:
        inner = Text(justify="center")
        inner.append("T R U S T", style=f"bold {C_BLUE}")
        inner.append("  5\n", style=f"bold {C_AMBER}")
        inner.append("autonomous agent", style=C_DIM)
        return Panel(inner, border_style=C_CHROME, box=ROUNDED)


# ─── Model / Provider Info ───────────────────────────────────────────────────


class SidebarInfo(Static):
    """Model, provider, token, and context info panel."""

    model_name: reactive[str] = reactive("")
    provider: reactive[str] = reactive("")
    token_info: reactive[str] = reactive("")
    context_info: reactive[str] = reactive("")

    def render(self) -> Text:
        t = Text()
        if self.model_name:
            t.append("  model ", style=C_DIM)
            t.append(f"{self.model_name}\n", style=f"bold {C_BLUE}")
        if self.provider:
            t.append("  via   ", style=C_DIM)
            t.append(f"{self.provider}\n", style=C_SECONDARY)
        if self.token_info:
            t.append("  tok   ", style=C_DIM)
            t.append(f"{self.token_info}\n", style=C_SECONDARY)
        if self.context_info:
            t.append("  ctx   ", style=C_DIM)
            t.append(f"{self.context_info}\n", style=C_AMBER)
        if not t.plain.strip():
            t.append("  waiting...\n", style=C_DIM)
        return t


# ─── Watchdog ────────────────────────────────────────────────────────────────


class WatchdogHeader(Static):
    """Section divider with watchdog label."""

    def render(self) -> Text:
        t = Text()
        t.append("  \u25c9", style=f"bold {C_PURPLE}")
        t.append(" WATCHDOG", style=f"bold {C_PURPLE}")
        return t


class WatchdogLog(RichLog):
    """Scrollable watchdog narrative area in the sidebar."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.auto_scroll = True
        self._last_narrative: str = ""

    def add_narrative(self, content: str, level: str = "ok") -> None:
        """Add a watchdog narrative entry.

        Args:
            content: Narrative text (may contain markdown for warn/error).
            level: One of 'ok', 'warn', 'error', 'start'.
        """
        if content == self._last_narrative:
            return
        self._last_narrative = content

        color_map: dict[str, str] = {
            "ok": C_DIM_PURPLE,
            "warn": C_PURPLE,
            "error": C_RED,
            "start": C_PURPLE,
        }
        color = color_map.get(level, C_DIM_PURPLE)

        if level in ("warn", "error") and len(content) > 40:
            border = C_RED if level == "error" else C_PURPLE
            self.write(
                Panel(
                    Markdown(content),
                    border_style=border,
                    box=ROUNDED,
                )
            )
        else:
            t = Text()
            t.append("\u25c9 ", style=f"bold {color}")
            t.append(content, style=color)
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
