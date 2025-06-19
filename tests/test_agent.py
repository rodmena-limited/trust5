from __future__ import annotations
import json
from unittest.mock import MagicMock, patch
import pytest
from trust5.core.agent import MAX_HISTORY_MESSAGES, MAX_TOOL_RESULT_LENGTH, Agent, _truncate
from trust5.core.llm import LLM, LLMError
_PATCHES = [
    "trust5.core.agent.emit",
    "trust5.core.agent.emit_block",
]

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

def test_agent_returns_content_when_no_tool_calls(_emit, _emit_block):
    """When the LLM responds with content and no tool_calls, run() returns that content."""
    llm = make_mock_llm([_resp(content="Hello from LLM")])
    agent = _make_agent(llm)
    result = agent.run("Say hello")
    assert result == "Hello from LLM"
    llm.chat.assert_called_once()

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

def test_agent_max_turns_no_content_returns_default(_emit, _emit_block):
    """When max_turns is reached and no content was ever produced, return default message."""
    tool_resp = _resp(content="", tool_calls=[_tool_call("Read", {"file_path": "f.txt"})])
    llm = make_mock_llm([tool_resp] * 3)
    agent = _make_agent(llm)

    with patch.object(agent.tools, "read_file", return_value="data"):
        result = agent.run("go", max_turns=2)

    assert result == "Agent completed all turns without final response."

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
