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
