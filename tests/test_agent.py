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
