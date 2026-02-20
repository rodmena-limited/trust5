"""Centralized timeout and limit constants for the Trust5 pipeline.

Instead of magic numbers scattered across files, all configurable timeouts
and limits are defined here with descriptive names.  These are defaults —
many can be overridden via stage context at runtime.
"""

# ── Agent execution ──────────────────────────────────────────────────────────
AGENT_MAX_TURNS = 20
AGENT_MAX_HISTORY_MESSAGES = 60
AGENT_TOOL_RESULT_LIMIT = 8000
AGENT_DEFAULT_TIMEOUT = 1800  # 30 min wall-clock per agent run
AGENT_PER_TURN_TIMEOUT = 600  # 10 min ceiling per single LLM call
AGENT_IDLE_WARN_TURNS = 5  # warn after N consecutive read-only turns
AGENT_IDLE_MAX_TURNS = 10  # abort agent after N consecutive read-only turns

# ── Repair / validate loop ───────────────────────────────────────────────────
MAX_REPAIR_ATTEMPTS = 5
CONSECUTIVE_FAILURE_LIMIT = 3  # escalate to failed_continue after N identical failures
MAX_REIMPLEMENTATIONS = 3
TEST_OUTPUT_LIMIT = 4000
REPAIR_AGENT_TIMEOUT = 600  # 10 min per repair attempt
QUICK_TEST_TIMEOUT = 60  # pre/post-flight check
PYTEST_PER_TEST_TIMEOUT = 30  # per-test timeout via pytest-timeout plugin

# ── Quality gate ─────────────────────────────────────────────────────────────
MAX_QUALITY_ATTEMPTS = 3

# ── Workflow-level timeouts ──────────────────────────────────────────────────
TIMEOUT_PLAN = 600.0  # 10 min for plan phase
TIMEOUT_DEVELOP = 7200.0  # 2 hr for full develop pipeline
TIMEOUT_RUN = 1200.0  # 20 min for run-from-spec
TIMEOUT_LOOP = 3600.0  # 1 hr for diagnostics loop

# ── Subprocess execution ─────────────────────────────────────────────────────
BASH_TIMEOUT = 120  # LLM-invoked bash commands
GREP_TIMEOUT = 60  # grep search
SYNTAX_CHECK_TIMEOUT = 120  # compileall / go vet
TEST_RUN_TIMEOUT = 120  # pytest / go test

# ── LLM streaming ───────────────────────────────────────────────────────────
# Per-chunk read timeout varies by whether thinking mode is active.
# Ollama with thinking=True can pause 3-5 min between chunks while reasoning.
STREAM_READ_TIMEOUT_THINKING = 600  # 10 min per chunk (thinking models)
STREAM_READ_TIMEOUT_STANDARD = 120  # 2 min per chunk (non-thinking)
STREAM_TOTAL_TIMEOUT = 900  # 15 min total ceiling for any stream

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
