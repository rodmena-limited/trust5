"""Tests for resilient-circuit integration in trust5/core/llm.py."""

from __future__ import annotations

from unittest.mock import patch

from resilient_circuit import CircuitState

from trust5.core.llm import (
    _BACKOFF_CONNECT,
    _BACKOFF_SERVER,
    _get_model_circuit,
    _model_circuits,
)

# ── ExponentialDelay strategy tests ──────────────────────────────────


def test_backoff_connect_first_attempt_range():
    """First attempt delay should be in [0, 2*5] = [0, 10] seconds."""
    delays = [_BACKOFF_CONNECT.for_attempt(1) for _ in range(50)]
    assert all(0 <= d <= 10.0 for d in delays), f"Out of range: {min(delays):.2f} - {max(delays):.2f}"

    assert max(delays) - min(delays) > 1.0, "Delays should be jittered"


def test_backoff_connect_later_attempts_grow():
    """Later attempts should produce larger average delays."""
    early = [_BACKOFF_CONNECT.for_attempt(1) for _ in range(100)]
    late = [_BACKOFF_CONNECT.for_attempt(5) for _ in range(100)]
    assert sum(late) / len(late) > sum(early) / len(early)


def test_backoff_connect_max_capped():
    """Delays should never exceed max_delay (300s)."""
    delays = [_BACKOFF_CONNECT.for_attempt(20) for _ in range(50)]
    assert all(d <= 300.0 for d in delays)


def test_backoff_server_first_attempt_range():
    """Server backoff starts at 10s base, so first attempt in [0, 20]."""
    delays = [_BACKOFF_SERVER.for_attempt(1) for _ in range(50)]
    assert all(0 <= d <= 20.0 for d in delays)


def test_backoff_server_max_capped():
    """Server delays should also cap at 300s."""
    delays = [_BACKOFF_SERVER.for_attempt(20) for _ in range(50)]
    assert all(d <= 300.0 for d in delays)


# ── Circuit breaker registry tests ───────────────────────────────────


def test_get_model_circuit_creates_new():
    """_get_model_circuit creates a new circuit for unknown models."""
    model = "test-model-unique-abc123"
    try:
        circuit = _get_model_circuit(model)
        assert circuit is not None
        assert circuit.status == CircuitState.CLOSED
    finally:
        _model_circuits.pop(model, None)


def test_get_model_circuit_caches():
    """Same model name returns the same circuit instance."""
    model = "test-model-cache-xyz789"
    try:
        c1 = _get_model_circuit(model)
        c2 = _get_model_circuit(model)
        assert c1 is c2
    finally:
        _model_circuits.pop(model, None)


def test_get_model_circuit_different_models():
    """Different model names get different circuits."""
    m1, m2 = "test-model-diff-aaa", "test-model-diff-bbb"
    try:
        c1 = _get_model_circuit(m1)
        c2 = _get_model_circuit(m2)
        assert c1 is not c2
    finally:
        _model_circuits.pop(m1, None)
        _model_circuits.pop(m2, None)


# ── Circuit breaker integration tests ────────────────────────────────


@patch("trust5.core.llm.emit")
def test_chat_skips_open_circuit(mock_emit):
    """When a model's circuit is OPEN, chat() should skip it and try fallback."""
    from trust5.core.llm import LLM

    llm = LLM(model="primary-model", fallback_models=["fallback-model"])

    primary_circuit = _get_model_circuit("primary-model")
    for _ in range(5):
        primary_circuit._status.mark_failure()
        primary_circuit._save_state()
    assert primary_circuit.status == CircuitState.OPEN

    try:
        with patch.object(llm, "_chat_with_retry") as mock_retry:
            mock_retry.return_value = {"message": {"role": "assistant", "content": "ok"}}
            result = llm.chat([{"role": "user", "content": "test"}])
            assert result == {"message": {"role": "assistant", "content": "ok"}}

            assert mock_retry.call_args[0][2] == "fallback-model"

        afbk_calls = [c for c in mock_emit.call_args_list if c[0][0].value == "AFBK"]
        assert any("Circuit open" in str(c) for c in afbk_calls)
    finally:
        _model_circuits.pop("primary-model", None)
        _model_circuits.pop("fallback-model", None)


@patch("trust5.core.llm.emit")
def test_chat_marks_success_on_circuit(mock_emit):
    """Successful chat() call marks success on the model's circuit."""
    from trust5.core.llm import LLM

    llm = LLM(model="success-test-model")
    circuit = _get_model_circuit("success-test-model")

    try:
        with patch.object(llm, "_chat_with_retry") as mock_retry:
            mock_retry.return_value = {"message": {"role": "assistant", "content": "ok"}}
            llm.chat([{"role": "user", "content": "test"}])

            assert circuit.status == CircuitState.CLOSED
    finally:
        _model_circuits.pop("success-test-model", None)


@patch("trust5.core.llm.emit")
def test_chat_all_circuits_open_raises(mock_emit):
    """When all model circuits are open, chat() raises LLMError."""
    from trust5.core.llm import LLM, LLMError

    llm = LLM(model="all-open-primary", fallback_models=["all-open-fallback"])

    for model_name in ("all-open-primary", "all-open-fallback"):
        cb = _get_model_circuit(model_name)
        for _ in range(5):
            cb._status.mark_failure()
            cb._save_state()

    try:
        import pytest

        with pytest.raises(LLMError, match="All models exhausted"):
            llm.chat([{"role": "user", "content": "test"}])
    finally:
        _model_circuits.pop("all-open-primary", None)
        _model_circuits.pop("all-open-fallback", None)


# ── _chat_with_retry backoff integration tests ───────────────────────


@patch("trust5.core.llm.emit")
def test_chat_with_retry_uses_connect_backoff(mock_emit):
    """_chat_with_retry uses _BACKOFF_CONNECT for connection errors."""
    from trust5.core.llm import LLM, LLMError

    llm = LLM(model="retry-connect-model")
    _model_circuits.pop("retry-connect-model", None)

    call_count = 0

    def fake_do_chat(messages, tools, model, timeout):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise LLMError("connection failed", retryable=True, error_class="connection")
        return {"message": {"role": "assistant", "content": "ok"}}

    try:
        with (
            patch.object(llm, "_do_chat", side_effect=fake_do_chat),
            patch.object(llm._abort, "wait", return_value=False) as mock_wait,
        ):
            result = llm._chat_with_retry([{"role": "user", "content": "test"}], None, "retry-connect-model", 300)
            assert result == {"message": {"role": "assistant", "content": "ok"}}
            assert call_count == 3

            assert mock_wait.call_count == 2

            for call in mock_wait.call_args_list:
                delay = call[1]["timeout"]
                assert 0 <= delay <= 300.0, f"Delay {delay} out of range"
    finally:
        _model_circuits.pop("retry-connect-model", None)


@patch("trust5.core.llm.emit")
def test_chat_with_retry_abort_interrupts_sleep(mock_emit):
    """When _abort is set during retry sleep, _chat_with_retry re-raises."""
    from trust5.core.llm import LLM, LLMError

    llm = LLM(model="abort-test-model")
    _model_circuits.pop("abort-test-model", None)

    def fake_do_chat(messages, tools, model, timeout):
        raise LLMError("server error", retryable=True, error_class="server")

    try:
        with (
            patch.object(llm, "_do_chat", side_effect=fake_do_chat),
            patch.object(llm._abort, "wait", return_value=True),
        ):
            import pytest

            with pytest.raises(LLMError, match="server error"):
                llm._chat_with_retry([{"role": "user", "content": "test"}], None, "abort-test-model", 300)
    finally:
        _model_circuits.pop("abort-test-model", None)
