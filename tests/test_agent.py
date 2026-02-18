"""Tests for trust5.core.agent module."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests.exceptions

from trust5.core.agent import MAX_HISTORY_MESSAGES, MAX_TOOL_RESULT_LENGTH, Agent, _truncate
from trust5.core.llm import LLM, LLMError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_mock_llm(responses: list[dict]) -> MagicMock:
    """Create a mock LLM that returns responses in sequence."""
    llm = MagicMock(spec=LLM)
    llm.model = "test-model"
    llm.chat = MagicMock(side_effect=responses)
    return llm


def _resp(content: str = "", tool_calls: list | None = None) -> dict:
    """Shortcut to build an LLM chat response dict."""
    return {"message": {"content": content, "tool_calls": tool_calls or []}}


def _tool_call(name: str, arguments: str | dict, call_id: str = "tc-1") -> dict:
    """Shortcut to build a tool_call entry."""
    if isinstance(arguments, dict):
        arguments = json.dumps(arguments)
    return {"id": call_id, "function": {"name": name, "arguments": arguments}}


def _make_agent(llm: MagicMock, **kwargs) -> Agent:
    """Create an Agent with common defaults and all event emitters mocked."""
    defaults = dict(name="test-agent", prompt="You are a test agent.", llm=llm)
    defaults.update(kwargs)
    return Agent(**defaults)


# Patch targets — silence event emitters during tests.
_PATCHES = [
    "trust5.core.agent.emit",
    "trust5.core.agent.emit_block",
]


# ---------------------------------------------------------------------------
# _truncate()
# ---------------------------------------------------------------------------


def test_truncate_short_text():
    """Text shorter than the limit is returned unchanged."""
    text = "Hello, world!"
    assert _truncate(text) == text


def test_truncate_exact_limit():
    """Text exactly at the limit is returned unchanged."""
    text = "x" * MAX_TOOL_RESULT_LENGTH
    assert _truncate(text) == text


def test_truncate_long_text():
    """Text exceeding the limit gets middle-truncated with a marker."""
    text = "A" * (MAX_TOOL_RESULT_LENGTH + 200)
    result = _truncate(text)
    assert len(result) < len(text)
    assert "chars truncated" in result
    # The result should start with the first half and end with the last half.
    half = MAX_TOOL_RESULT_LENGTH // 2
    assert result.startswith("A" * half)
    assert result.endswith("A" * half)


def test_truncate_custom_limit():
    """Truncation works with a custom max_len parameter."""
    text = "B" * 100
    result = _truncate(text, max_len=40)
    assert "chars truncated" in result
    assert result.startswith("B" * 20)
    assert result.endswith("B" * 20)


# ---------------------------------------------------------------------------
# Agent.run() — basic conversation
# ---------------------------------------------------------------------------


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_agent_returns_content_when_no_tool_calls(_emit, _emit_block):
    """When the LLM responds with content and no tool_calls, run() returns that content."""
    llm = make_mock_llm([_resp(content="Hello from LLM")])
    agent = _make_agent(llm)
    result = agent.run("Say hello")
    assert result == "Hello from LLM"
    llm.chat.assert_called_once()


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_agent_dispatches_tool_calls(_emit, _emit_block):
    """When the LLM returns a tool_call, the agent executes it and feeds the result back."""
    responses = [
        _resp(content="", tool_calls=[_tool_call("Read", {"file_path": "/tmp/test.txt"})]),
        _resp(content="I read the file for you."),
    ]
    llm = make_mock_llm(responses)
    agent = _make_agent(llm)

    with patch.object(agent.tools, "read_file", return_value="file contents") as mock_read:
        result = agent.run("Read a file")

    assert result == "I read the file for you."
    mock_read.assert_called_once_with("/tmp/test.txt", offset=None, limit=None)
    assert llm.chat.call_count == 2


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_agent_max_turns_reached(_emit, _emit_block):
    """Agent stops after max_turns and returns last content or default message."""
    # Every turn produces a tool call so the agent never gets a clean finish.
    tool_resp = _resp(content="partial", tool_calls=[_tool_call("Read", {"file_path": "f.txt"})])
    llm = make_mock_llm([tool_resp] * 5)
    agent = _make_agent(llm)

    with patch.object(agent.tools, "read_file", return_value="data"):
        result = agent.run("keep going", max_turns=3)

    # Should have called chat exactly max_turns times.
    assert llm.chat.call_count == 3
    # last_content was set to "partial" on each turn.
    assert result == "partial"


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_agent_max_turns_no_content_returns_default(_emit, _emit_block):
    """When max_turns is reached and no content was ever produced, return default message."""
    tool_resp = _resp(content="", tool_calls=[_tool_call("Read", {"file_path": "f.txt"})])
    llm = make_mock_llm([tool_resp] * 3)
    agent = _make_agent(llm)

    with patch.object(agent.tools, "read_file", return_value="data"):
        result = agent.run("go", max_turns=2)

    assert result == "Agent completed all turns without final response."


# ---------------------------------------------------------------------------
# Agent.run() — LLMError handling
# ---------------------------------------------------------------------------


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_agent_llm_error_returns_last_content(_emit, _emit_block):
    """If LLMError occurs on turn 2 but turn 1 produced content, return that content."""
    responses = [
        _resp(content="turn 1 answer", tool_calls=[_tool_call("Read", {"file_path": "a.txt"})]),
        LLMError("server down", retryable=True, error_class="server"),
    ]
    llm = make_mock_llm(responses)
    agent = _make_agent(llm)

    with patch.object(agent.tools, "read_file", return_value="ok"):
        result = agent.run("do something")

    assert result == "turn 1 answer"


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_agent_llm_error_raises_when_no_content(_emit, _emit_block):
    """If LLMError occurs on turn 1 with no prior content, the error is re-raised."""
    llm = make_mock_llm([LLMError("auth failed", error_class="permanent")])
    agent = _make_agent(llm)

    with pytest.raises(LLMError, match="auth failed"):
        agent.run("hello")


# ---------------------------------------------------------------------------
# Agent._handle_tool_call() — malformed JSON
# ---------------------------------------------------------------------------


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_handle_malformed_json_args(_emit, _emit_block):
    """Malformed JSON arguments return an error string to the LLM (not empty dict)."""
    bad_tc = _tool_call("Read", "not valid json {{{")
    # Turn 1: tool call with bad JSON. Turn 2: LLM acknowledges.
    responses = [
        _resp(content="", tool_calls=[bad_tc]),
        _resp(content="I see there was an error."),
    ]
    llm = make_mock_llm(responses)
    agent = _make_agent(llm)
    result = agent.run("read something")

    assert result == "I see there was an error."
    # The second chat call should have received the error message as a tool result.
    second_call_messages = llm.chat.call_args_list[1][0][0]
    tool_result_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert len(tool_result_msgs) == 1
    assert "Invalid JSON arguments" in tool_result_msgs[0]["content"]


# ---------------------------------------------------------------------------
# Agent._handle_tool_call() — unknown tool falls through to MCP
# ---------------------------------------------------------------------------


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_unknown_tool_falls_through_to_mcp(_emit, _emit_block):
    """An unknown tool name triggers MCP fallback when MCP clients are present."""
    mock_mcp = MagicMock()
    mock_mcp.list_tools.return_value = []
    mock_mcp.call_tool.return_value = "mcp result data"

    responses = [
        _resp(content="", tool_calls=[_tool_call("CustomMcpTool", {"key": "val"})]),
        _resp(content="Got the MCP result."),
    ]
    llm = make_mock_llm(responses)
    agent = _make_agent(llm, mcp_clients=[mock_mcp])
    result = agent.run("use custom tool")

    assert result == "Got the MCP result."
    mock_mcp.call_tool.assert_called_once_with("CustomMcpTool", {"key": "val"})


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_unknown_tool_no_mcp_returns_error(_emit, _emit_block):
    """An unknown tool with no MCP clients returns 'Unknown tool' error string."""
    responses = [
        _resp(content="", tool_calls=[_tool_call("NonExistent", {"a": 1})]),
        _resp(content="Tool not found."),
    ]
    llm = make_mock_llm(responses)
    agent = _make_agent(llm)
    result = agent.run("call nonexistent")

    assert result == "Tool not found."
    # Check the tool result message sent back to LLM.
    second_call_messages = llm.chat.call_args_list[1][0][0]
    tool_result_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert any("Unknown tool" in m["content"] for m in tool_result_msgs)


# ---------------------------------------------------------------------------
# Agent._execute_tool() — exception handling
# ---------------------------------------------------------------------------


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_tool_error_returns_error_string(_emit, _emit_block):
    """An OSError in a tool handler returns an error string, not a crash."""
    responses = [
        _resp(content="", tool_calls=[_tool_call("Read", {"file_path": "/no/such/file"})]),
        _resp(content="File not found, noted."),
    ]
    llm = make_mock_llm(responses)
    agent = _make_agent(llm)

    with patch.object(agent.tools, "read_file", side_effect=OSError("No such file")):
        result = agent.run("read missing file")

    assert result == "File not found, noted."
    # Verify the error was returned as a tool result, not raised.
    second_call_messages = llm.chat.call_args_list[1][0][0]
    tool_result_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert len(tool_result_msgs) == 1
    assert "Tool Read error" in tool_result_msgs[0]["content"]
    assert "No such file" in tool_result_msgs[0]["content"]


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_tool_valueerror_returns_error_string(_emit, _emit_block):
    """A ValueError in a tool handler is caught and returned as an error string."""
    responses = [
        _resp(content="", tool_calls=[_tool_call("Write", {"file_path": "f.txt", "content": "x"})]),
        _resp(content="Write failed, understood."),
    ]
    llm = make_mock_llm(responses)
    agent = _make_agent(llm)

    with patch.object(agent.tools, "write_file", side_effect=ValueError("bad value")):
        result = agent.run("write file")

    assert result == "Write failed, understood."
    second_call_messages = llm.chat.call_args_list[1][0][0]
    tool_result_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert any("Tool Write error" in m["content"] for m in tool_result_msgs)


# ---------------------------------------------------------------------------
# Agent._trim_history_if_needed()
# ---------------------------------------------------------------------------


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_trim_history_no_trim_when_under_limit(_emit, _emit_block):
    """History is not trimmed when message count is at or below MAX_HISTORY_MESSAGES."""
    llm = make_mock_llm([_resp(content="done")])
    agent = _make_agent(llm)
    agent.run("hello")
    # After one exchange: 1 user message + 1 assistant message = 2 messages.
    assert len(agent.history) == 2


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_trim_history(_emit, _emit_block):
    """When history exceeds MAX_HISTORY_MESSAGES, it is trimmed from the front."""
    # Build a scenario with many tool calls to inflate history beyond the limit.
    # Each turn adds: 1 assistant message + 1 tool result = 2 messages.
    # Plus the initial user message = 1.
    # We need > MAX_HISTORY_MESSAGES (60). With 35 turns of tool calls, we get
    # 1 (user) + 35 * (1 assistant + 1 tool) = 71 messages.
    num_tool_turns = 35
    tool_responses = [
        _resp(content="", tool_calls=[_tool_call("Read", {"file_path": "f.txt"}, call_id=f"tc-{i}")])
        for i in range(num_tool_turns)
    ]
    # Final response with no tool calls.
    tool_responses.append(_resp(content="all done"))

    llm = make_mock_llm(tool_responses)
    # Use allowed_tools=["Read"] so idle detection (which tracks write-tool
    # usage) is bypassed — this test exercises trimming, not idle detection.
    agent = _make_agent(llm, allowed_tools=["Read"])

    with patch.object(agent.tools, "read_file", return_value="data"):
        result = agent.run("process many files", max_turns=num_tool_turns + 1)

    assert result == "all done"
    # Trimming happens after tool-call turns. The final turn (no tool calls)
    # adds 1 assistant message without triggering trim, so we allow +1 slack.
    assert len(agent.history) <= MAX_HISTORY_MESSAGES + 1
    # Without any trimming, history would be 1 (user) + 35*2 (assistant+tool) + 1 (final) = 72.
    # Verify trimming actually reduced the count.
    untrimmed_count = 1 + num_tool_turns * 2 + 1
    assert len(agent.history) < untrimmed_count


# ---------------------------------------------------------------------------
# Agent — tool_call_id propagation
# ---------------------------------------------------------------------------


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_tool_call_id_propagated(_emit, _emit_block):
    """The tool_call_id from the LLM response is included in the tool result message."""
    responses = [
        _resp(content="", tool_calls=[_tool_call("Read", {"file_path": "a.txt"}, call_id="call-42")]),
        _resp(content="done"),
    ]
    llm = make_mock_llm(responses)
    agent = _make_agent(llm)

    with patch.object(agent.tools, "read_file", return_value="content"):
        agent.run("read")

    second_call_messages = llm.chat.call_args_list[1][0][0]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call-42"


# ---------------------------------------------------------------------------
# Agent — multiple tool calls in single turn
# ---------------------------------------------------------------------------


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_multiple_tool_calls_in_single_turn(_emit, _emit_block):
    """Multiple tool_calls in a single LLM response are all dispatched."""
    responses = [
        _resp(
            content="",
            tool_calls=[
                _tool_call("Read", {"file_path": "a.txt"}, call_id="tc-a"),
                _tool_call("Read", {"file_path": "b.txt"}, call_id="tc-b"),
            ],
        ),
        _resp(content="Read both files."),
    ]
    llm = make_mock_llm(responses)
    agent = _make_agent(llm)

    with patch.object(agent.tools, "read_file", return_value="data") as mock_read:
        result = agent.run("read two files")

    assert result == "Read both files."
    assert mock_read.call_count == 2
    mock_read.assert_any_call("a.txt", offset=None, limit=None)
    mock_read.assert_any_call("b.txt", offset=None, limit=None)


# ---------------------------------------------------------------------------
# Agent — non_interactive auto-answer
# ---------------------------------------------------------------------------


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_ask_user_non_interactive_auto_answers(_emit, _emit_block):
    """In non_interactive mode, AskUserQuestion returns the first option automatically."""
    responses = [
        _resp(
            content="", tool_calls=[_tool_call("AskUserQuestion", {"question": "Continue?", "options": ["yes", "no"]})]
        ),
        _resp(content="User said yes."),
    ]
    llm = make_mock_llm(responses)
    agent = _make_agent(llm, non_interactive=True)
    result = agent.run("ask the user")

    assert result == "User said yes."
    # The tool result should be "yes" (auto-answered).
    second_call_messages = llm.chat.call_args_list[1][0][0]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert tool_msgs[0]["content"] == "yes"


# ---------------------------------------------------------------------------
# Agent — _summarize_args coverage
# ---------------------------------------------------------------------------


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_summarize_args_bash(_emit, _emit_block):
    """_summarize_args for Bash includes the command."""
    agent = _make_agent(make_mock_llm([]))
    summary = agent._summarize_args("Bash", {"command": "ls -la"})
    assert "ls -la" in summary


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_summarize_args_glob(_emit, _emit_block):
    """_summarize_args for Glob includes the pattern."""
    agent = _make_agent(make_mock_llm([]))
    summary = agent._summarize_args("Glob", {"pattern": "**/*.py"})
    assert "**/*.py" in summary


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_summarize_args_unknown_tool(_emit, _emit_block):
    """_summarize_args for unknown tools lists first 3 keys."""
    agent = _make_agent(make_mock_llm([]))
    summary = agent._summarize_args("SomeTool", {"alpha": 1, "beta": 2})
    assert "alpha=..." in summary
    assert "beta=..." in summary


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_summarize_args_empty(_emit, _emit_block):
    """_summarize_args with empty args returns empty string."""
    agent = _make_agent(make_mock_llm([]))
    summary = agent._summarize_args("SomeTool", {})
    assert summary == ""


# ---------------------------------------------------------------------------
# Agent — history message structure
# ---------------------------------------------------------------------------


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_history_structure_after_run(_emit, _emit_block):
    """After a simple run, history contains the user message and assistant reply."""
    llm = make_mock_llm([_resp(content="reply")])
    agent = _make_agent(llm)
    agent.run("hi")

    assert len(agent.history) == 2
    assert agent.history[0] == {"role": "user", "content": "hi"}
    assert agent.history[1]["content"] == "reply"


# ---------------------------------------------------------------------------
# Agent — tool result truncation in run loop
# ---------------------------------------------------------------------------


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_tool_result_truncated_in_run(_emit, _emit_block):
    """Large tool results are truncated before being sent back to the LLM."""
    big_output = "X" * (MAX_TOOL_RESULT_LENGTH + 500)
    responses = [
        _resp(content="", tool_calls=[_tool_call("Read", {"file_path": "big.txt"})]),
        _resp(content="Got it."),
    ]
    llm = make_mock_llm(responses)
    agent = _make_agent(llm)

    with patch.object(agent.tools, "read_file", return_value=big_output):
        result = agent.run("read big file")

    assert result == "Got it."
    # Check that the tool result in the second call was truncated.
    second_call_messages = llm.chat.call_args_list[1][0][0]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert "chars truncated" in tool_msgs[0]["content"]
    assert len(tool_msgs[0]["content"]) < len(big_output)


# ---------------------------------------------------------------------------
# Agent — MCP tool listing failure is non-fatal
# ---------------------------------------------------------------------------


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_mcp_list_tools_failure_non_fatal(_emit, _emit_block):
    """If an MCP client's list_tools() raises, the agent still initializes."""
    mock_mcp = MagicMock()
    mock_mcp.list_tools.side_effect = RuntimeError("MCP server unreachable")

    llm = make_mock_llm([_resp(content="ok")])
    # Should not raise during construction.
    agent = _make_agent(llm, mcp_clients=[mock_mcp])
    result = agent.run("hello")
    assert result == "ok"


# ---------------------------------------------------------------------------
# Agent — MCP call_tool failure falls through
# ---------------------------------------------------------------------------


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_mcp_call_tool_failure_falls_through(_emit, _emit_block):
    """If all MCP clients fail call_tool, 'Unknown tool' error is returned."""
    mock_mcp = MagicMock()
    mock_mcp.list_tools.return_value = []
    mock_mcp.call_tool.side_effect = RuntimeError("MCP call failed")

    responses = [
        _resp(content="", tool_calls=[_tool_call("McpTool", {"x": 1})]),
        _resp(content="No luck."),
    ]
    llm = make_mock_llm(responses)
    agent = _make_agent(llm, mcp_clients=[mock_mcp])
    result = agent.run("try mcp")

    assert result == "No luck."
    second_call_messages = llm.chat.call_args_list[1][0][0]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert any("Unknown tool" in m["content"] for m in tool_msgs)


# ---------------------------------------------------------------------------
# Agent.run() — wall-clock timeout
# ---------------------------------------------------------------------------


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_agent_timeout_stops_at_turn_boundary(_emit, _emit_block):
    """Agent.run() stops when wall-clock timeout is exceeded between turns."""
    # Simulate time progressing: first call returns 0, then jumps past deadline.
    call_count = 0
    base_time = 1000.0

    def mock_monotonic():
        nonlocal call_count
        call_count += 1
        # First two calls: deadline computation + first turn check -> within budget
        if call_count <= 2:
            return base_time
        # Subsequent calls: past deadline
        return base_time + 999.0

    tool_resp = _resp(content="partial", tool_calls=[_tool_call("Read", {"file_path": "f.txt"})])
    llm = make_mock_llm([tool_resp] * 10)
    agent = _make_agent(llm)

    with patch.object(agent.tools, "read_file", return_value="data"), patch("trust5.core.agent.time") as mock_time:
        mock_time.monotonic = mock_monotonic
        result = agent.run("keep going", max_turns=10, timeout_seconds=30)

    # Should have stopped early due to timeout, not exhausted all 10 turns.
    assert llm.chat.call_count < 10
    assert result == "partial"


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_agent_no_timeout_when_none(_emit, _emit_block):
    """Agent.run() with timeout_seconds=None does not enforce a deadline."""
    llm = make_mock_llm([_resp(content="done")])
    agent = _make_agent(llm)
    result = agent.run("hello", timeout_seconds=None)
    assert result == "done"
    llm.chat.assert_called_once()


# ---------------------------------------------------------------------------
# Agent — per-turn watchdog timer
# ---------------------------------------------------------------------------


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_watchdog_calls_abort_and_reset(_emit, _emit_block):
    """Each turn calls reset_abort() before chat and starts a watchdog timer."""
    llm = make_mock_llm([_resp(content="done")])
    agent = _make_agent(llm)

    result = agent.run("hello", timeout_seconds=600)

    assert result == "done"
    # reset_abort() should have been called at least once (before the LLM call)
    llm.reset_abort.assert_called()


# ---------------------------------------------------------------------------
# Agent — idle detection (warn + abort)
# ---------------------------------------------------------------------------


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_idle_detection_aborts_after_max_read_only_turns(_emit, _emit_block):
    """Agent with write tools is aborted after AGENT_IDLE_MAX_TURNS read-only turns."""
    from trust5.core.constants import AGENT_IDLE_MAX_TURNS

    # Build enough read-only turns to trigger idle abort, then a final "done".
    num_turns = AGENT_IDLE_MAX_TURNS + 5
    responses = [
        _resp(content="", tool_calls=[_tool_call("Read", {"file_path": "f.txt"}, call_id=f"tc-{i}")])
        for i in range(num_turns)
    ]
    # If idle abort works, this "done" should never be reached.
    responses.append(_resp(content="done"))

    llm = make_mock_llm(responses)
    agent = _make_agent(llm)  # default tools include Write, Edit, Bash

    with patch.object(agent.tools, "read_file", return_value="data"):
        agent.run("explore the codebase", max_turns=num_turns + 1)

    # Should have been aborted by idle detection, not reached "done".
    assert llm.chat.call_count <= AGENT_IDLE_MAX_TURNS + 1
    # One of the emit calls should contain "Idle abort"
    idle_abort_emitted = any("Idle abort" in str(call) for call in _emit.call_args_list)
    assert idle_abort_emitted, "Expected idle abort warning to be emitted"


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_idle_detection_skipped_for_planner(_emit, _emit_block):
    """Read-only agents (no write tools) are exempt from idle detection."""
    from trust5.core.constants import AGENT_IDLE_MAX_TURNS

    num_turns = AGENT_IDLE_MAX_TURNS + 2
    responses = [
        _resp(content="", tool_calls=[_tool_call("Read", {"file_path": "f.txt"}, call_id=f"tc-{i}")])
        for i in range(num_turns)
    ]
    responses.append(_resp(content="analysis complete"))

    llm = make_mock_llm(responses)
    agent = _make_agent(llm, allowed_tools=["Read", "Grep", "Glob"])

    with patch.object(agent.tools, "read_file", return_value="data"):
        result = agent.run("analyze codebase", max_turns=num_turns + 1)

    # Planner should NOT be aborted — should reach "analysis complete".
    assert result == "analysis complete"
    assert llm.chat.call_count == num_turns + 1


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_idle_counter_resets_on_write(_emit, _emit_block):
    """Consecutive read-only counter resets when a write tool is used."""
    from trust5.core.constants import AGENT_IDLE_WARN_TURNS

    # Pattern: (WARN-1) read-only turns, then 1 write turn, then (WARN-1) more read-only, then done.
    # Total read-only consecutive never reaches WARN threshold.
    read_batch = AGENT_IDLE_WARN_TURNS - 1
    responses = []
    for i in range(read_batch):
        responses.append(_resp(content="", tool_calls=[_tool_call("Read", {"file_path": "f.txt"}, call_id=f"r1-{i}")]))
    # Write turn resets counter
    responses.append(
        _resp(content="", tool_calls=[_tool_call("Write", {"file_path": "out.txt", "content": "x"}, call_id="w1")])
    )
    for i in range(read_batch):
        responses.append(_resp(content="", tool_calls=[_tool_call("Read", {"file_path": "f.txt"}, call_id=f"r2-{i}")]))
    responses.append(_resp(content="done"))

    llm = make_mock_llm(responses)
    agent = _make_agent(llm)

    with (
        patch.object(agent.tools, "read_file", return_value="data"),
        patch.object(agent.tools, "write_file", return_value="ok"),
    ):
        result = agent.run("work", max_turns=len(responses))

    assert result == "done"
    # No idle warning should have been emitted
    idle_warned = any("No file changes" in str(call) for call in _emit.call_args_list)
    assert not idle_warned, "Should not warn when write resets the counter"


# ---------------------------------------------------------------------------
# Agent.run() — empty response retry
# ---------------------------------------------------------------------------


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_empty_response_retried_then_succeeds(_emit, _emit_block):
    """Empty response with no tool calls is retried; success on 2nd attempt."""
    responses = [
        _resp(content=""),  # empty — should be retried
        _resp(content="Recovered answer."),  # retry succeeds
    ]
    llm = make_mock_llm(responses)
    agent = _make_agent(llm)
    result = agent.run("say something")

    assert result == "Recovered answer."
    assert llm.chat.call_count == 2
    # The empty assistant message should have been removed before the retry.
    # After retry, history has: user + assistant("Recovered answer.")
    assert len(agent.history) == 2


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_empty_response_falls_back_to_last_content(_emit, _emit_block):
    """After exhausting empty-response retries, falls back to last_content from earlier turn."""
    from trust5.core.agent import _MAX_EMPTY_RESPONSE_RETRIES

    responses = [
        # Turn 1: content + tool call
        _resp(content="Good progress", tool_calls=[_tool_call("Read", {"file_path": "a.txt"})]),
        # Turns 2+: persistent empty responses (exceeds retry limit)
        *[_resp(content="") for _ in range(_MAX_EMPTY_RESPONSE_RETRIES + 1)],
    ]
    llm = make_mock_llm(responses)
    agent = _make_agent(llm)

    with patch.object(agent.tools, "read_file", return_value="data"):
        result = agent.run("do work", max_turns=_MAX_EMPTY_RESPONSE_RETRIES + 5)

    # Should fall back to the last non-empty content.
    assert result == "Good progress"
    empty_fallback_emitted = any("last non-empty response" in str(c) for c in _emit.call_args_list)
    assert empty_fallback_emitted


@patch("trust5.core.agent.emit_block")
@patch("trust5.core.agent.emit")
def test_empty_response_accepted_after_retries_no_last_content(_emit, _emit_block):
    """When no prior content exists and retries exhausted, empty string is returned."""
    from trust5.core.agent import _MAX_EMPTY_RESPONSE_RETRIES

    responses = [_resp(content="") for _ in range(_MAX_EMPTY_RESPONSE_RETRIES + 1)]
    llm = make_mock_llm(responses)
    agent = _make_agent(llm)
    result = agent.run("say something")

    # All retries exhausted and no last_content — returns empty.
    assert result == ""
    assert llm.chat.call_count == _MAX_EMPTY_RESPONSE_RETRIES + 1


# ---------------------------------------------------------------------------
# Mid-stream timeout → retryable LLMError (not TERMINAL)
# ---------------------------------------------------------------------------


class _ExplodingIterLines:
    """Simulates a response that starts streaming then dies with a network error."""

    def __init__(self, lines: list[str], error: Exception):
        self._lines = lines
        self._error = error

    def iter_lines(self, decode_unicode=True):
        yield from self._lines
        raise self._error

    def close(self):
        pass


def _make_bare_llm() -> LLM:
    """Create an LLM instance without making real network calls."""
    with patch.object(LLM, "__init__", lambda self, **kw: None):
        llm = LLM.__new__(LLM)
        llm.model = "test"
        llm.thinking_level = None
        llm._abort = __import__("threading").Event()
        return llm


def test_consume_stream_connection_error_becomes_retryable_llm_error():
    """Mid-stream ConnectionError should raise retryable LLMError, not crash."""
    llm = _make_bare_llm()
    fake_response = _ExplodingIterLines(
        lines=['{"message":{"content":"partial"},"done":false}'],
        error=requests.exceptions.ConnectionError("Read timed out."),
    )
    with pytest.raises(LLMError) as exc_info:
        llm._consume_stream(fake_response, "test-model")

    assert exc_info.value.retryable is True
    assert exc_info.value.error_class == "server"


def test_consume_stream_read_timeout_becomes_retryable_llm_error():
    """Mid-stream ReadTimeout should raise retryable LLMError."""
    llm = _make_bare_llm()
    fake_response = _ExplodingIterLines(
        lines=[],
        error=requests.exceptions.ReadTimeout("Read timed out."),
    )
    with pytest.raises(LLMError) as exc_info:
        llm._consume_stream(fake_response, "test-model")

    assert exc_info.value.retryable is True
    assert exc_info.value.error_class == "server"


def test_consume_stream_os_error_becomes_retryable_llm_error():
    """Mid-stream OSError (broken pipe, etc.) should raise retryable LLMError."""
    llm = _make_bare_llm()
    fake_response = _ExplodingIterLines(
        lines=[],
        error=OSError("Broken pipe"),
    )
    with pytest.raises(LLMError) as exc_info:
        llm._consume_stream(fake_response, "test-model")

    assert exc_info.value.retryable is True
    assert exc_info.value.error_class == "connection"
