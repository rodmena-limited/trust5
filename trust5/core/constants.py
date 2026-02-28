"""Centralized timeout and limit constants for the Trust5 pipeline.

All values are read lazily from ``GlobalConfig`` (``~/.trust5/config.yaml``)
via Python's module-level ``__getattr__`` hook.  This means:

* No import-time side effects (config is read on first attribute access).
* ``from trust5.core.constants import AGENT_MAX_TURNS`` returns an ``int``
  — identical to the old hardcoded constants.
* The returned value is always *current* (not cached at import time), so
  hot-reloading the global config is transparently supported.

Environment variable overrides (``TRUST5_AGENT_MAX_TURNS=30``) and
per-project config are handled inside ``load_global_config()``.
"""

from __future__ import annotations

from typing import Any


def _cfg() -> Any:
    """Return the global config singleton (lazy import to avoid cycles)."""
    from .config import load_global_config

    return load_global_config()


# ── Module-level constants (sensible defaults, not config-driven) ────────────
# These are imported directly and do NOT go through __getattr__.

# Agent limits
AGENT_MAX_EMPTY_RETRIES = 3

# Subprocess defaults
DEFAULT_SUBPROCESS_TIMEOUT = 120
SUBPROCESS_TIMEOUT = DEFAULT_SUBPROCESS_TIMEOUT
DEFAULT_SETUP_TIMEOUT = 300

# Context builder limits
MAX_FILE_CONTENT = 6000
MAX_TOTAL_CONTEXT = 30000

# Error summarizer limits
MAX_RAW_ERROR_INPUT = 100_000
MAX_ERROR_SUMMARY = 20_000

# Quality thresholds (DO NOT change pillar weights)
QUALITY_OUTPUT_LIMIT = 6000
QUALITY_PASS_THRESHOLD = 0.70
PILLAR_PASS_THRESHOLD = 0.85
PILLAR_WARNING_THRESHOLD = 0.50

# TUI
TUI_BATCH_SIZE = 64
TUI_MAX_BLOCK_LINES = 500
TUI_MAX_THINKING_LINES = 50

# Tool safety limits
MAX_READ_FILE_SIZE = 1_048_576   # 1 MB
MAX_GLOB_RESULTS = 1_000
MAX_READFILES_COUNT = 100
MAX_READFILES_FILE_SIZE = 1_048_576  # 1 MB per file in ReadFiles
# Mutation testing
DEFAULT_MAX_MUTANTS = 10

# ── Attribute map: constant name → (config section, field name) ──────────────


_ATTR_MAP: dict[str, tuple[str, str]] = {
    # Agent execution
    "AGENT_MAX_TURNS": ("agent", "max_turns"),
    "AGENT_MAX_HISTORY_MESSAGES": ("agent", "max_history_messages"),
    "AGENT_TOOL_RESULT_LIMIT": ("agent", "tool_result_limit"),
    "AGENT_DEFAULT_TIMEOUT": ("agent", "default_timeout"),
    "AGENT_PER_TURN_TIMEOUT": ("agent", "per_turn_timeout"),
    "AGENT_IDLE_WARN_TURNS": ("agent", "idle_warn_turns"),
    "AGENT_IDLE_MAX_TURNS": ("agent", "idle_max_turns"),
    # Repair / validate loop
    "MAX_REPAIR_ATTEMPTS": ("pipeline", "max_repair_attempts"),
    "CONSECUTIVE_FAILURE_LIMIT": ("pipeline", "consecutive_failure_limit"),
    "MAX_REIMPLEMENTATIONS": ("pipeline", "max_reimplementations"),
    "MAX_RETRY_CYCLES": ("pipeline", "max_retry_cycles"),
    "TEST_OUTPUT_LIMIT": ("pipeline", "test_output_limit"),
    "REPAIR_AGENT_TIMEOUT": ("pipeline", "repair_agent_timeout"),
    "QUICK_TEST_TIMEOUT": ("pipeline", "quick_test_timeout"),
    "PYTEST_PER_TEST_TIMEOUT": ("pipeline", "pytest_per_test_timeout"),
    # Quality gate
    "MAX_QUALITY_ATTEMPTS": ("pipeline", "max_quality_attempts"),
    # Workflow-level timeouts
    "TIMEOUT_PLAN": ("timeouts", "plan"),
    "TIMEOUT_DEVELOP": ("timeouts", "develop"),
    "TIMEOUT_RUN": ("timeouts", "run"),
    "TIMEOUT_LOOP": ("timeouts", "loop"),
    # Subprocess execution
    "BASH_TIMEOUT": ("subprocess", "bash_timeout"),
    "GREP_TIMEOUT": ("subprocess", "grep_timeout"),
    "SYNTAX_CHECK_TIMEOUT": ("subprocess", "syntax_check_timeout"),
    "TEST_RUN_TIMEOUT": ("subprocess", "test_run_timeout"),
    # LLM streaming
    "STREAM_READ_TIMEOUT_THINKING": ("stream", "read_timeout_thinking"),
    "STREAM_READ_TIMEOUT_STANDARD": ("stream", "read_timeout_standard"),
    "STREAM_TOTAL_TIMEOUT": ("stream", "total_timeout"),
    "RETRY_DELAY_SERVER": ("stream", "retry_delay_server"),
    # MCP server
    "MCP_START_TIMEOUT": ("mcp", "start_timeout"),
    "MCP_PROCESS_STOP_TIMEOUT": ("mcp", "process_stop_timeout"),
    # Event bus
    "EVENT_BUS_SOCKET_TIMEOUT": ("event_bus", "socket_timeout"),
    "EVENT_QUEUE_BATCH_SIZE": ("event_bus", "queue_batch_size"),
    # TUI
    "TUI_MAX_LOG_LINES": ("tui", "max_log_lines"),
    "TUI_SPINNER_INTERVAL": ("tui", "spinner_interval"),
    "TUI_ELAPSED_TICK": ("tui", "elapsed_tick"),
    # Watchdog
    "WATCHDOG_CHECK_INTERVAL": ("watchdog", "check_interval"),
    "WATCHDOG_MAX_RUNTIME": ("watchdog", "max_runtime"),
    "WATCHDOG_OK_EMIT_INTERVAL": ("watchdog", "ok_emit_interval"),
    "WATCHDOG_MAX_LLM_AUDITS": ("watchdog", "max_llm_audits"),
    # Pipeline: additional
    "SETUP_TIMEOUT": ("pipeline", "setup_timeout"),
    "SUBPROCESS_TIMEOUT": ("pipeline", "subprocess_timeout"),
    # LLM provider
    "LLM_TIMEOUT_FAST": ("llm", "timeout_fast"),
    "LLM_TIMEOUT_STANDARD": ("llm", "timeout_standard"),
    "LLM_TIMEOUT_EXTENDED": ("llm", "timeout_extended"),
    "LLM_CONNECT_TIMEOUT": ("llm", "connect_timeout"),
    "LLM_TOKEN_REFRESH_MARGIN": ("llm", "token_refresh_margin"),
    "LLM_RETRY_BUDGET_CONNECT": ("llm", "retry_budget_connect"),
    "LLM_RETRY_BUDGET_SERVER": ("llm", "retry_budget_server"),
    "LLM_RETRY_BUDGET_RATE": ("llm", "retry_budget_rate"),
    "LLM_MAX_BACKOFF_DELAY": ("llm", "max_backoff_delay"),
}


def __getattr__(name: str) -> Any:
    """Module-level lazy attribute access — resolves constants from GlobalConfig."""
    if name in _ATTR_MAP:
        section, field = _ATTR_MAP[name]
        return getattr(getattr(_cfg(), section), field)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
