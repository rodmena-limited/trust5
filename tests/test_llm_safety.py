"""Tests for LLM safety improvements: context window validation (C5) and error classification (H7)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from trust5.core.llm import (
    LLM,
    LLMError,
    _model_circuits,
    _trim_messages_to_context,
    estimate_token_count,
)


# ── C5: estimate_token_count tests ─────────────────────────────────────


def test_estimate_token_count_basic():
    """Estimation uses ~4 chars per token heuristic."""
    messages = [{"role": "user", "content": "a" * 400}]
    assert estimate_token_count(messages) == 100


def test_estimate_token_count_empty_messages():
    """Empty message list returns 0 tokens."""
    assert estimate_token_count([]) == 0


def test_estimate_token_count_multiple_messages():
    """Token count sums content across all messages."""
    messages = [
        {"role": "system", "content": "a" * 200},
        {"role": "user", "content": "b" * 400},
        {"role": "assistant", "content": "c" * 200},
    ]
    # (200 + 400 + 200) / 4 = 200
    assert estimate_token_count(messages) == 200


# ── C5: Context window trimming tests ──────────────────────────────────


def test_trim_preserves_system_messages():
    """System messages are never removed during trimming."""
    messages = [
        {"role": "system", "content": "a" * 400},  # 100 tokens
        {"role": "user", "content": "b" * 400},  # 100 tokens
        {"role": "user", "content": "c" * 400},  # 100 tokens
    ]
    # Trim to 250 tokens -> must drop one non-system but keep system
    result = _trim_messages_to_context(messages, 250)
    system_msgs = [m for m in result if m["role"] == "system"]
    assert len(system_msgs) == 1
    assert system_msgs[0]["content"] == "a" * 400


def test_trim_keeps_last_non_system_message():
    """At least one non-system message is always preserved."""
    messages = [
        {"role": "system", "content": "x" * 400},
        {"role": "user", "content": "y" * 4000},  # 1000 tokens alone
    ]
    # Even with max_tokens=10, the last non-system message stays
    result = _trim_messages_to_context(messages, 10)
    non_system = [m for m in result if m["role"] != "system"]
    assert len(non_system) == 1


def test_trim_removes_oldest_non_system_first():
    """Oldest non-system messages are removed before newer ones."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old" * 100},  # 75 tokens
        {"role": "assistant", "content": "mid" * 100},  # 75 tokens
        {"role": "user", "content": "new" * 100},  # 75 tokens
    ]
    # Total ~225 tokens. Trim to 160 -> drops "old", keeps mid+new (150)
    result = _trim_messages_to_context(messages, 160)
    non_system = [m for m in result if m["role"] != "system"]
    assert len(non_system) == 2
    assert "old" not in non_system[0]["content"]
    assert "mid" in non_system[0]["content"]


@patch("trust5.core.llm.emit")
@patch("trust5.core.llm.MODEL_CONTEXT_WINDOW", {"test-small-ctx": 200})
def test_oversized_request_gets_trimmed(mock_emit):
    """Messages exceeding 90% of context window are trimmed before _do_chat."""
    llm = LLM(model="test-small-ctx")
    _model_circuits.pop("test-small-ctx", None)

    # 200 token context -> 90% = 180 tokens threshold
    # system: 10 tokens, 3 user msgs: 100 tokens each = 310 total > 180
    messages = [
        {"role": "system", "content": "s" * 40},
        {"role": "user", "content": "a" * 400},
        {"role": "user", "content": "b" * 400},
        {"role": "user", "content": "c" * 400},
    ]

    captured_messages: list[list[dict[str, str]]] = []

    def capture_do_chat(msgs, tools, model, timeout):
        captured_messages.append(msgs)
        return {"message": {"role": "assistant", "content": "ok"}}

    try:
        with (
            patch.object(llm, "_do_chat", side_effect=capture_do_chat),
            patch.object(llm._abort, "wait", return_value=False),
        ):
            llm._chat_with_retry(messages, None, "test-small-ctx", 300)

            assert len(captured_messages) == 1
            trimmed = captured_messages[0]
            # System message preserved
            assert any(m["role"] == "system" for m in trimmed)
            # Fewer non-system messages than original
            original_non_sys = [m for m in messages if m["role"] != "system"]
            trimmed_non_sys = [m for m in trimmed if m["role"] != "system"]
            assert len(trimmed_non_sys) < len(original_non_sys)
            # Estimated tokens within threshold
            assert estimate_token_count(trimmed) <= 180
    finally:
        _model_circuits.pop("test-small-ctx", None)


@patch("trust5.core.llm.emit")
def test_unknown_model_skips_context_validation(mock_emit):
    """Models not in MODEL_CONTEXT_WINDOW skip pre-validation (e.g. Ollama local)."""
    llm = LLM(model="my-custom-ollama-model")
    _model_circuits.pop("my-custom-ollama-model", None)

    # Huge message — should NOT be trimmed for unknown models
    messages = [{"role": "user", "content": "x" * 10_000_000}]

    captured_messages: list[list[dict[str, str]]] = []

    def capture_do_chat(msgs, tools, model, timeout):
        captured_messages.append(msgs)
        return {"message": {"role": "assistant", "content": "ok"}}

    try:
        with (
            patch.object(llm, "_do_chat", side_effect=capture_do_chat),
            patch.object(llm._abort, "wait", return_value=False),
        ):
            result = llm._chat_with_retry(messages, None, "my-custom-ollama-model", 300)
            assert result == {"message": {"role": "assistant", "content": "ok"}}
            # Messages passed through unmodified
            assert len(captured_messages[0]) == 1
            assert len(captured_messages[0][0]["content"]) == 10_000_000
    finally:
        _model_circuits.pop("my-custom-ollama-model", None)


# ── H7: Provider error classification tests ───────────────────────────


@patch("trust5.core.llm.emit")
def test_anthropic_invalid_request_classified_permanent(mock_emit):
    """Anthropic invalid_request_error is classified as permanent non-retryable."""
    llm = LLM(model="claude-opus-4-20250514", backend="anthropic")

    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = '{"type":"error","error":{"type":"invalid_request_error","message":"max_tokens: must be > 0"}}'
    mock_response.json.return_value = {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": "max_tokens: must be > 0"},
    }
    mock_response.headers = {}

    with (
        patch.object(llm, "_ensure_token_fresh"),
        patch.object(llm._session, "post", return_value=mock_response),
    ):
        with pytest.raises(LLMError) as exc_info:
            llm._post("https://api.anthropic.com/v1/messages", {}, "claude-opus-4-20250514", 300)
        assert exc_info.value.retryable is False
        assert exc_info.value.error_class == "permanent"
        assert "max_tokens" in str(exc_info.value)


@patch("trust5.core.llm.emit")
def test_400_error_includes_parsed_body_message(mock_emit):
    """400-level errors include the parsed error message from response body."""
    llm = LLM(model="test-model")

    mock_response = MagicMock()
    mock_response.status_code = 422
    mock_response.text = '{"error":{"message":"Invalid tool schema","code":"validation_error"}}'
    mock_response.json.return_value = {
        "error": {"message": "Invalid tool schema", "code": "validation_error"},
    }
    mock_response.headers = {}

    with (
        patch.object(llm, "_ensure_token_fresh"),
        patch.object(llm._session, "post", return_value=mock_response),
    ):
        with pytest.raises(LLMError) as exc_info:
            llm._post("http://localhost/api", {}, "test-model", 300)
        assert "Invalid tool schema" in str(exc_info.value)
        assert exc_info.value.error_class == "permanent"


@patch("trust5.core.llm.emit")
def test_400_error_unparseable_body_uses_text(mock_emit):
    """When response body can't be parsed as JSON, fall back to raw text."""
    llm = LLM(model="test-model")

    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = "Bad Request: malformed input"
    mock_response.json.side_effect = ValueError("No JSON")
    mock_response.headers = {}

    with (
        patch.object(llm, "_ensure_token_fresh"),
        patch.object(llm._session, "post", return_value=mock_response),
    ):
        with pytest.raises(LLMError) as exc_info:
            llm._post("http://localhost/api", {}, "test-model", 300)
        assert "Bad Request" in str(exc_info.value)
        assert exc_info.value.retryable is False
