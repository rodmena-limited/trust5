import json
import logging
import subprocess
import threading
import time
from typing import Any
from .constants import (
    AGENT_IDLE_MAX_TURNS,
    AGENT_IDLE_WARN_TURNS,
    AGENT_MAX_HISTORY_MESSAGES,
    AGENT_PER_TURN_TIMEOUT,
    AGENT_TOOL_RESULT_LIMIT,
)
from .llm import LLM, LLMError
from .mcp import MCPClient, MCPSSEClient
from .message import M, emit, emit_block
from .tools import Tools
logger = logging.getLogger(__name__)
MAX_TOOL_RESULT_LENGTH = AGENT_TOOL_RESULT_LIMIT
MAX_HISTORY_MESSAGES = AGENT_MAX_HISTORY_MESSAGES
_WRITE_TOOLS = frozenset({"Write", "Edit", "Bash"})
_MAX_EMPTY_RESPONSE_RETRIES = 2

def _safe_int(value: object, default: int | None = None) -> int | None:
    """Coerce an LLM tool argument to int, tolerating str/float/None.

    LLMs frequently send ``"10"`` (string), ``10.0`` (float), or omit
    optional parameters entirely.  This helper handles all cases without
    crashing on unexpected types.
    """
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return int(float(value))
            except (ValueError, OverflowError):
                return default
    return default
