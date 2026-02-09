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

def _truncate(text: str, max_len: int = MAX_TOOL_RESULT_LENGTH) -> str:
    if len(text) <= max_len:
        return text
    half = max_len // 2
    return (
        text[:half] + f"\n... [{len(text) - max_len} chars truncated — "
        f"use Read with offset/limit to read specific line ranges, "
        f"or use Grep to find the relevant lines first] ...\n" + text[-half:]
    )

class Agent:
    def __init__(
        self,
        name: str,
        prompt: str,
        llm: LLM,
        mcp_clients: list[MCPClient | MCPSSEClient] | None = None,
        non_interactive: bool = False,
        allowed_tools: list[str] | None = None,
        owned_files: list[str] | None = None,
        denied_files: list[str] | None = None,
        deny_test_patterns: bool = False,
    ):
        self.name = name
        self.system_prompt = prompt
        self.llm = llm
        self.mcp_clients = mcp_clients or []
        self.non_interactive = non_interactive
        self.history: list[dict[str, str]] = []
        self.tools = Tools(
            owned_files=owned_files,
            denied_files=denied_files,
            deny_test_patterns=deny_test_patterns,
        )
        self.tool_definitions = self.tools.get_definitions(
            non_interactive=self.non_interactive,
            allowed_tools=allowed_tools,
        )

        for client in self.mcp_clients:
            try:
                mcp_tools = client.list_tools()
                for tool in mcp_tools:
                    self.tool_definitions.append(
                        {
                            "type": "function",
                            "function": {
                                "name": tool["name"],
                                "description": tool.get("description", ""),
                                "parameters": tool.get("inputSchema", {}),
                            },
                        }
                    )
            except Exception as e:
                emit(M.SWRN, f"[{self.name}] Failed to load MCP tools: {e}")
