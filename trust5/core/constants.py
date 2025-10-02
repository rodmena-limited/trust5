AGENT_MAX_TURNS = 20
AGENT_MAX_HISTORY_MESSAGES = 60
AGENT_TOOL_RESULT_LIMIT = 8000
AGENT_DEFAULT_TIMEOUT = 1800  # 30 min wall-clock per agent run
AGENT_PER_TURN_TIMEOUT = 600  # 10 min ceiling per single LLM call
AGENT_IDLE_WARN_TURNS = 5  # warn after N consecutive read-only turns
AGENT_IDLE_MAX_TURNS = 10  # abort agent after N consecutive read-only turns
MAX_REPAIR_ATTEMPTS = 5
