from typing import Any

from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static

# Re-export from submodules for backward compatibility.
# app.py imports these names from .widgets — keep them available here.
from .log_widget import Trust5Log, _format_count, _parse_kv  # noqa: F401
from .theme import (
    C_AMBER,
    C_BG,
    C_BLUE,
    C_CHROME,
    C_DIM,
    C_GREEN,
    C_LAVENDER,
    C_MUTED,
    C_SECONDARY,
    C_TEAL,
    STATUS_BAR_ONLY,  # noqa: F401
    get_theme,  # noqa: F401
)

__all__ = [
    # Re-exports
    "Trust5Log",
    "_format_count",
    "_parse_kv",
    "STATUS_BAR_ONLY",
    "get_theme",
    # Defined here
    "HeaderWidget",
    "StatusBar0",
    "StatusBar1",
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

    # Phases that repeat per-module in parallel pipelines.
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
        # Refresh is handled by _route_batch() in app.py — no per-call refresh needed.

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

        # Refresh is handled by _route_batch() in app.py — no per-call refresh needed.

    @staticmethod
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


# ─── Status Bars ─────────────────────────────────────────────────────────────


class StatusBar1(Static):
    """Upper status bar: stage, elapsed, turn, tool, files, thinking/waiting."""

    stage_name: reactive[str] = reactive("")
    elapsed: reactive[str] = reactive("")
    turn_info: reactive[str] = reactive("")
    current_tool: reactive[str] = reactive("")
    files_changed: reactive[int] = reactive(0)
    thinking: reactive[bool] = reactive(False)
    waiting: reactive[bool] = reactive(False)

    # Braille spinner — 10 frames at 80ms = smooth 12.5 FPS rotation
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

        # Keybindings
        text.append("  ", style=C_DIM)
        text.append("^C", style=C_SECONDARY)
        text.append(" quit  ", style=C_DIM)
        text.append("c", style=C_SECONDARY)
        text.append(" clear  ", style=C_DIM)
        text.append("s", style=C_SECONDARY)
        text.append(" scroll", style=C_DIM)
        return text
