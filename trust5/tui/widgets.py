from typing import Any

from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static

from .log_widget import Trust5Log, _format_count, _parse_kv  # noqa: F401
from .sidebar import Sidebar, SidebarInfo, WatchdogLog  # noqa: F401
from .theme import (
    C_AMBER,
    C_BG,
    C_BLUE,
    C_CHROME,
    C_DIM,
    C_DIM_RED,
    C_GREEN,
    C_LAVENDER,
    C_MUTED,
    C_SECONDARY,
    C_TEAL,
    STATUS_BAR_ONLY,  # noqa: F401
    get_theme,  # noqa: F401
)

__all__ = [
    "Trust5Log",
    "_format_count",
    "_parse_kv",
    "STATUS_BAR_ONLY",
    "get_theme",
    "HeaderWidget",
    "StatusBar0",
    "StatusBar1",
    "Sidebar",
    "SidebarInfo",
    "WatchdogLog",
]


# ─── Header ──────────────────────────────────────────────────────────────────


class HeaderWidget(Static):
    """Pipeline progress header with module-aware stage badges.

    Tracks per-module completion for each phase so that parallel pipelines
    show accurate progress (e.g. "TEST 2/3" = 2 of 3 modules done).
    A phase badge turns green only when ALL modules have completed it.
    """

    STAGES = [
        ("plan", "PLAN"),
        ("write_tests", "TEST"),
        ("implement", "CODE"),
        ("validate", "VERIFY"),
        ("repair", "FIX"),
        ("review", "REVIEW"),
        ("quality", "GATE"),
    ]

    _MODULE_PHASES: frozenset[str] = frozenset(
        {
            "write_tests",
            "implement",
            "validate",
            "repair",
        }
    )

    current_stage: reactive[str] = reactive("plan")
    completed_stages: reactive[set[str]] = reactive(set)
    failed_stages: reactive[set[str]] = reactive(set)
    stage_total: reactive[int] = reactive(0)
    stages_done: reactive[int] = reactive(0)
    module_count: reactive[int] = reactive(0)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._phase_modules: dict[str, set[str]] = {}
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
        mc = self.module_count
        if mc > 0 and len(self._phase_modules[phase_key]) >= mc:
            self.completed_stages = self.completed_stages | {phase_key}

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
        cur_idx = self._stage_index(self.current_stage)
        if status == "success":
            self.completed_stages = self.completed_stages | {key}
            self.failed_stages = self.failed_stages - {key}
        elif status == "failed":
            if key in self.completed_stages:
                self.completed_stages = self.completed_stages - {key}
            self.failed_stages = self.failed_stages | {key}
        elif status == "running":
            # Clear failed state when stage restarts (repair cycles).
            self.failed_stages = self.failed_stages - {key}
        new_idx = self._stage_index(key)
        allow_backward = key in ("validate", "repair", "review", "quality")
        if new_idx >= cur_idx or allow_backward:
            self.current_stage = key

    @staticmethod
    def _match_stage_key(ref: str) -> str | None:
        if "test-writer" in ref or "test_writer" in ref or "write test" in ref:
            return "write_tests"
        if "implement" in ref:
            return "implement"
        if "validate" in ref or "run tests" in ref:
            return "validate"
        if "repair" in ref or "fix failure" in ref:
            return "repair"
        if "review" in ref:
            return "review"
        if "quality" in ref or "trust 5" in ref:
            return "quality"
        if "plan" in ref or "planner" in ref:
            return "plan"
        return None

    def render(self) -> Text:
        text = Text()
        text.append(" TRUST5 ", style=f"bold {C_BG} on {C_BLUE}")

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
            elif key in self.failed_stages and key != self.current_stage:
                # Failed stage (advisory, e.g. review FAILED_CONTINUE) — dim red ✗
                text.append(f" \u2717 {label} ", style=C_DIM_RED)
            elif key == self.current_stage:
                if show_frac:
                    text.append(f" {label} {done}/{mc} ", style=f"bold {C_BG} on {C_LAVENDER}")
                else:
                    text.append(f" {label} ", style=f"bold {C_BG} on {C_LAVENDER}")
            elif show_frac and done > 0:
                text.append(f" {label} {done}/{mc} ", style=C_SECONDARY)
            else:
                text.append(f" {label} ", style=C_MUTED)

        return text


# ─── Status Bars ─────────────────────────────────────────────────────────────


class StatusBar1(Static):
    stage_name: reactive[str] = reactive("")
    current_tool: reactive[str] = reactive("")
    def render(self) -> Text:
        left = Text()
        if self.stage_name:
            left.append(f" {self.stage_name}", style=f"bold {C_LAVENDER}")
        else:
            left.append(" idle", style=C_DIM)
        if self.current_tool:
            left.append("  \u25b8 ", style=C_DIM)
            left.append(self.current_tool, style=f"bold {C_TEAL}")
        right = Text()
        right.append("^C", style=C_SECONDARY)
        right.append(" quit  ", style=C_DIM)
        right.append("c", style=C_SECONDARY)
        right.append(" clear  ", style=C_DIM)
        right.append("s", style=C_SECONDARY)
        right.append(" scroll ", style=C_DIM)
        try:
            w = self.size.width
        except Exception:
            w = 120
        gap = max(w - len(left.plain) - len(right.plain), 1)
        left.append(" " * gap)
        left.append_text(right)
        return left



class StatusBar0(Static):
    """Lower status bar: model, tokens, context, keybindings."""

    model_name: reactive[str] = reactive("")
    provider: reactive[str] = reactive("")
    token_info: reactive[str] = reactive("")
    context_info: reactive[str] = reactive("")

    def render(self) -> Text:
        text = Text()
        if self.model_name:
            text.append(f" {self.model_name}", style=f"bold {C_BLUE}")
        else:
            text.append(" Trust5", style=f"bold {C_BLUE}")

        if self.provider:
            text.append(f"  {self.provider}", style=C_MUTED)

        if self.token_info:
            text.append(f"  {self.token_info}", style=C_SECONDARY)
        if self.context_info:
            text.append(f"  {self.context_info}", style=C_AMBER)

        text.append("  ", style=C_DIM)
        text.append("^C", style=C_SECONDARY)
        text.append(" quit  ", style=C_DIM)
        text.append("c", style=C_SECONDARY)
        text.append(" clear  ", style=C_DIM)
        text.append("s", style=C_SECONDARY)
        text.append(" scroll", style=C_DIM)
        return text
