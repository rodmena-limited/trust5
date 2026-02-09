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

    def run(self, user_input: str, max_turns: int = 20, timeout_seconds: float | None = None) -> str:
        deadline = (time.monotonic() + timeout_seconds) if timeout_seconds else None
        self.history.append({"role": "user", "content": user_input})
        messages = [{"role": "system", "content": self.system_prompt}] + self.history

        emit_block(
            M.CSYS,
            f"{self.name} system prompt ({len(self.system_prompt)} chars)",
            self.system_prompt,
            max_lines=40,
        )
        emit_block(
            M.CUSR,
            f"{self.name} user input ({len(user_input)} chars)",
            user_input,
            max_lines=30,
        )
        emit(
            M.CMDL,
            f"[{self.name}] model={self.llm.model} tools={len(self.tool_definitions)}",
        )

        # Idle detection: track consecutive turns with no write-tool calls.
        # Read-only agents (planners) are exempt — they have no write tools.
        tool_names_available = {td.get("function", {}).get("name", "") for td in self.tool_definitions}
        has_write_tools = bool(tool_names_available & _WRITE_TOOLS)
        consecutive_read_only = 0

        last_content = ""
        empty_response_retries = 0
        prev_msg_count = 0
        for i in range(max_turns):
            if deadline is not None and time.monotonic() > deadline:
                emit(
                    M.SWRN,
                    f"[{self.name}] Wall-clock timeout ({timeout_seconds:.0f}s) reached "
                    f"at turn {i + 1}/{max_turns}. Returning last response.",
                )
                break

            emit(
                M.ATRN,
                f"[{self.name}] Turn {i + 1}/{max_turns} (history={len(self.history)} msgs)",
            )
            # Only emit full context on first turn
            if i == 0:
                self._emit_context(messages, prev_msg_count)
            prev_msg_count = len(messages)

            # Per-turn watchdog: abort the LLM call if a single turn takes
            # too long.  The watchdog sets an abort flag that the stream
            # consumer checks between chunks.
            remaining = (deadline - time.monotonic()) if deadline else float("inf")
            per_turn = min(remaining / 2, AGENT_PER_TURN_TIMEOUT)

            self.llm.reset_abort()
            watchdog = threading.Timer(per_turn, self.llm.abort)
            watchdog.daemon = True
            watchdog.start()
            try:
                response = self.llm.chat(messages, tools=self.tool_definitions)
            except LLMError as e:
                emit(M.AERR, f"[{self.name}] LLM failed on turn {i + 1}: {e}")
                if last_content:
                    return last_content
                raise
            finally:
                watchdog.cancel()

            message = response.get("message", {})
            content: str = message.get("content", "")
            tool_calls = message.get("tool_calls", [])

            if content:
                last_content = content

            self.history.append(message)
            messages.append(message)

            if not tool_calls:
                if not content and empty_response_retries < _MAX_EMPTY_RESPONSE_RETRIES:
                    empty_response_retries += 1
                    emit(
                        M.SWRN,
                        f"[{self.name}] Empty response with no tool calls "
                        f"(retry {empty_response_retries}/{_MAX_EMPTY_RESPONSE_RETRIES})",
                    )
                    # Remove the empty assistant message so the LLM sees a
                    # clean conversation on the next attempt.
                    self.history.pop()
                    messages.pop()
                    continue
                if not content and last_content:
                    emit(
                        M.SWRN,
                        f"[{self.name}] Empty final response — "
                        f"returning last non-empty response ({len(last_content)} chars)",
                    )
                    return last_content
                emit(M.ASUM, f"[{self.name}] Final response ({len(content)} chars)")
                return content

            for tc in tool_calls:
                result = self._handle_tool_call(tc)
                truncated = _truncate(str(result))
                tool_name = tc.get("function", {}).get("name", "unknown")
                tool_msg: dict[str, Any] = {
                    "role": "tool",
                    "content": truncated,
                    "name": tool_name,
                }
                if tc.get("id"):
                    tool_msg["tool_call_id"] = tc["id"]
                emit(M.CTLR, f"[{self.name}] {tool_name} result ({len(truncated)} chars)")
                self.history.append(tool_msg)
                messages.append(tool_msg)

            # Idle detection: did this turn make any write-tool calls?
            if has_write_tools and tool_calls:
                turn_wrote = any(tc.get("function", {}).get("name", "") in _WRITE_TOOLS for tc in tool_calls)
                if turn_wrote:
                    consecutive_read_only = 0
                else:
                    consecutive_read_only += 1
                    if consecutive_read_only == AGENT_IDLE_WARN_TURNS:
                        emit(
                            M.SWRN,
                            f"[{self.name}] No file changes for {consecutive_read_only} consecutive turns",
                        )
                    if consecutive_read_only >= AGENT_IDLE_MAX_TURNS:
                        emit(
                            M.SWRN,
                            f"[{self.name}] Idle abort — no file changes for {consecutive_read_only} turns",
                        )
                        break

            self._trim_history_if_needed(messages)

        if last_content:
            return last_content
        return "Agent completed all turns without final response."

    def _handle_tool_call(self, tool_call: dict[str, Any]) -> str:
        function = tool_call.get("function", {})
        name = function.get("name", "")
        args_str = function.get("arguments", "{}")

        try:
            args = json.loads(args_str) if isinstance(args_str, str) else args_str
        except (json.JSONDecodeError, TypeError):
            emit(M.SWRN, f"[{self.name}] Invalid JSON arguments for {name}: {str(args_str)[:200]}")
            return f"Invalid JSON arguments for {name}: {str(args_str)[:200]}"

        args_summary = self._summarize_args(name, args)
        emit(M.CTLC, f"[{self.name}] {name}: {args_summary}")

        try:
            result = self._execute_tool(name, args)
        except ValueError:
            result = None  # Unknown tool — fall through to MCP

        if result is not None:
            emit(M.TRES, f"[{self.name}] {name} -> {len(str(result))} chars")
            return result

        for client in self.mcp_clients:
            try:
                mcp_result = str(client.call_tool(name, args))
                emit(M.TRES, f"[{self.name}] {name} (MCP) -> {len(mcp_result)} chars")
                return mcp_result
            except Exception:
                continue

        emit(M.AERR, f"[{self.name}] Unknown tool: {name}")
        return f"Unknown tool: {name}"
