"""Centralized timeout and limit constants for the Trust5 pipeline.

Instead of magic numbers scattered across files, all configurable timeouts
and limits are defined here with descriptive names.  These are defaults —
many can be overridden via stage context at runtime.
"""

# ── Agent execution ──────────────────────────────────────────────────────────
AGENT_MAX_TURNS = 20
AGENT_MAX_HISTORY_MESSAGES = 60
AGENT_TOOL_RESULT_LIMIT = 8000
AGENT_DEFAULT_TIMEOUT = 7200  # 2 hr wall-clock per agent run (large codebases may need full compile cycles)
AGENT_PER_TURN_TIMEOUT = 1800  # 30 min ceiling per single LLM call (large codebases need more reasoning)
AGENT_IDLE_WARN_TURNS = 5  # warn after N consecutive read-only turns
AGENT_IDLE_MAX_TURNS = 10  # abort agent after N consecutive read-only turns

# ── Repair / validate loop ───────────────────────────────────────────────────
MAX_REPAIR_ATTEMPTS = 5
CONSECUTIVE_FAILURE_LIMIT = 3  # escalate to failed_continue after N identical failures
MAX_REIMPLEMENTATIONS = 3
TEST_OUTPUT_LIMIT = 4000
REPAIR_AGENT_TIMEOUT = 1800  # 30 min per repair attempt (matches agent turn timeout)
QUICK_TEST_TIMEOUT = 60  # pre/post-flight check
PYTEST_PER_TEST_TIMEOUT = 30  # per-test timeout via pytest-timeout plugin

# ── Quality gate ─────────────────────────────────────────────────────────────
MAX_QUALITY_ATTEMPTS = 3

# ── Workflow-level timeouts ──────────────────────────────────────────────────
TIMEOUT_PLAN = 3600.0  # 1 hr for plan phase (complex multi-module plans need more time)
TIMEOUT_DEVELOP = 864000.0  # 10 days for full develop pipeline (big projects run for days/weeks)
TIMEOUT_RUN = 86400.0  # 1 day for run-from-spec
TIMEOUT_LOOP = 86400.0  # 1 day for diagnostics loop

# ── Subprocess execution ─────────────────────────────────────────────────────
BASH_TIMEOUT = 600  # 10 min — LLM-invoked bash commands (compiling large projects)
GREP_TIMEOUT = 60  # grep search
SYNTAX_CHECK_TIMEOUT = 300  # 5 min — compileall / go vet on large codebases
TEST_RUN_TIMEOUT = 600  # 10 min — pytest / go test for comprehensive suites

# ── LLM streaming ───────────────────────────────────────────────────────────
# Per-chunk read timeout varies by whether thinking mode is active.
# Ollama with thinking=True can pause 3-5 min between chunks while reasoning.
STREAM_READ_TIMEOUT_THINKING = 600  # 10 min per chunk (thinking models)
STREAM_READ_TIMEOUT_STANDARD = 120  # 2 min per chunk (non-thinking)
STREAM_TOTAL_TIMEOUT = 3600  # 1 hr total ceiling for any stream (matches agent turn increase)

# ── MCP server ───────────────────────────────────────────────────────────────
MCP_START_TIMEOUT = 30.0
MCP_PROCESS_STOP_TIMEOUT = 5

# ── Event bus ────────────────────────────────────────────────────────────────
EVENT_BUS_SOCKET_TIMEOUT = 5.0
EVENT_QUEUE_BATCH_SIZE = 64

# ── TUI ──────────────────────────────────────────────────────────────────────
TUI_MAX_LOG_LINES = 5000
TUI_SPINNER_INTERVAL = 0.08  # seconds between spinner frames
TUI_ELAPSED_TICK = 1.0  # seconds between elapsed updates
