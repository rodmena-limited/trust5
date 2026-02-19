"""Tests for LLM authentication error handling."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import requests.exceptions

from trust5.core.auth.provider import TokenData
from trust5.core.llm import LLM, LLMError
from trust5.core.llm_errors import LLMError as LLMErrorDirect

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm(**kwargs) -> LLM:
    """Create an LLM with defaults and suppress emit calls."""
    defaults = dict(
        model="test-model",
        base_url="http://localhost:11434",
        backend="ollama",
    )
    defaults.update(kwargs)
    with patch("trust5.core.llm.emit"):
        return LLM(**defaults)


# Patch targets — silence event emitters during tests.
_EMIT_PATCH = "trust5.core.llm.emit"


# ---------------------------------------------------------------------------
# 1. test_auth_error_breaks_fallback_chain
# ---------------------------------------------------------------------------


@patch(_EMIT_PATCH)
def test_auth_error_breaks_fallback_chain(mock_emit: MagicMock) -> None:
    """Auth errors must NOT try fallback models — same credentials fail everywhere."""
    llm = _make_llm(fallback_models=["m2", "m3"])

    with patch.object(
        llm,
        "_do_chat",
        side_effect=LLMError(
            "Auth failed",
            retryable=False,
            error_class="auth",
        ),
    ) as mock_do_chat:
        with pytest.raises(LLMError) as exc_info:
            llm.chat([{"role": "user", "content": "hi"}])

        assert exc_info.value.error_class == "auth"
        # _do_chat should be called exactly once (primary model only, no fallbacks).
        assert mock_do_chat.call_count == 1


# ---------------------------------------------------------------------------
# 2. test_auth_error_class_properties
# ---------------------------------------------------------------------------


def test_auth_error_class_properties() -> None:
    """LLMError with error_class='auth' has correct property values."""
    err = LLMErrorDirect("Unauthorized", retryable=True, error_class="auth")
    assert err.is_auth_error is True
    assert err.is_network_error is False
    assert err.retryable is True

    # Non-retryable variant
    err2 = LLMErrorDirect("Forbidden", retryable=False, error_class="auth")
    assert err2.is_auth_error is True
    assert err2.retryable is False


# ---------------------------------------------------------------------------
# 3. test_connection_error_still_breaks_fallback
# ---------------------------------------------------------------------------


@patch(_EMIT_PATCH)
def test_connection_error_still_breaks_fallback(mock_emit: MagicMock) -> None:
    """Regression: error_class='connection' also breaks fallback chain."""
    llm = _make_llm(fallback_models=["m2", "m3"])

    with patch.object(
        llm,
        "_do_chat",
        side_effect=LLMError(
            "Connection refused",
            retryable=False,
            error_class="connection",
        ),
    ) as mock_do_chat:
        with pytest.raises(LLMError) as exc_info:
            llm.chat([{"role": "user", "content": "hi"}])

        assert exc_info.value.error_class == "connection"
        assert mock_do_chat.call_count == 1


# ---------------------------------------------------------------------------
# 4. test_server_error_tries_fallback
# ---------------------------------------------------------------------------


@patch(_EMIT_PATCH)
def test_server_error_tries_fallback(mock_emit: MagicMock) -> None:
    """Server errors (error_class='server') should try all fallback models."""
    llm = _make_llm(fallback_models=["m2", "m3"])

    # Bypass the retry loop: make _chat_with_retry just call _do_chat once.
    def fake_chat_with_retry(messages, tools, model, timeout):
        return llm._do_chat(messages, tools, model, timeout)

    with patch.object(llm, "_chat_with_retry", side_effect=fake_chat_with_retry):
        with patch.object(
            llm,
            "_do_chat",
            side_effect=LLMError(
                "Server error",
                retryable=True,
                error_class="server",
            ),
        ) as mock_do_chat:
            with pytest.raises(LLMError):
                llm.chat([{"role": "user", "content": "hi"}])

            # Should try primary + 2 fallbacks = 3 calls
            assert mock_do_chat.call_count == 3


# ---------------------------------------------------------------------------
# 5. test_refresh_retries_on_transient
# ---------------------------------------------------------------------------


@patch(_EMIT_PATCH)
def test_refresh_retries_on_transient(mock_emit: MagicMock) -> None:
    """_try_refresh_token_locked retries on ConnectionError, succeeds on 3rd attempt."""
    llm = _make_llm(provider_name="google", auth_header="Authorization")

    old_token = TokenData(
        access_token="old_at",
        refresh_token="old_rt",
        expires_at=time.time() + 3600,
    )
    new_token = TokenData(
        access_token="new_at",
        refresh_token="new_rt",
        expires_at=time.time() + 3600,
    )

    mock_provider = MagicMock()
    # First 2 calls raise ConnectionError, 3rd succeeds
    mock_provider.refresh.side_effect = [
        requests.exceptions.ConnectionError("network down"),
        requests.exceptions.ConnectionError("still down"),
        new_token,
    ]

    mock_store = MagicMock()
    mock_store.load.return_value = old_token

    with (
        patch("trust5.core.auth.registry.get_provider", return_value=mock_provider),
        patch("trust5.core.auth.token_store.TokenStore", return_value=mock_store),
        patch("time.sleep"),  # skip actual sleeps
    ):
        result = llm._try_refresh_token_locked()

    assert result is True
    assert mock_provider.refresh.call_count == 3
    mock_store.save.assert_called_once_with("google", new_token)
    # Verify session header was updated
    assert "Bearer new_at" in llm._session.headers.get("Authorization", "")


# ---------------------------------------------------------------------------
# 6. test_refresh_stops_on_permanent
# ---------------------------------------------------------------------------


@patch(_EMIT_PATCH)
def test_refresh_stops_on_permanent(mock_emit: MagicMock) -> None:
    """_try_refresh_token_locked stops immediately on HTTPError (permanent)."""
    llm = _make_llm(provider_name="google", auth_header="Authorization")

    old_token = TokenData(
        access_token="old_at",
        refresh_token="old_rt",
        expires_at=time.time() + 3600,
    )

    mock_provider = MagicMock()
    http_resp = MagicMock()
    http_resp.status_code = 400
    mock_provider.refresh.side_effect = requests.exceptions.HTTPError(
        response=http_resp,
    )

    mock_store = MagicMock()
    mock_store.load.return_value = old_token

    with (
        patch("trust5.core.auth.registry.get_provider", return_value=mock_provider),
        patch("trust5.core.auth.token_store.TokenStore", return_value=mock_store),
    ):
        result = llm._try_refresh_token_locked()

    assert result is False
    assert mock_provider.refresh.call_count == 1
    mock_store.save.assert_not_called()


# ---------------------------------------------------------------------------
# 7. test_ensure_token_fresh_thread_safety
# ---------------------------------------------------------------------------


@patch(_EMIT_PATCH)
def test_ensure_token_fresh_thread_safety(mock_emit: MagicMock) -> None:
    """Multiple threads calling _ensure_token_fresh serialize via lock.

    With an expired token, only the first thread to acquire the lock should
    trigger a refresh; subsequent threads re-check and see the fresh token.
    """
    llm = _make_llm(provider_name="google", auth_header="Authorization")

    expired_token = TokenData(
        access_token="old_at",
        refresh_token="old_rt",
        expires_at=time.time() - 100,  # already expired
    )
    fresh_token = TokenData(
        access_token="new_at",
        refresh_token="new_rt",
        expires_at=time.time() + 3600,  # good for an hour
    )

    refresh_call_count = 0
    refresh_lock = threading.Lock()

    def mock_refresh_locked() -> bool:
        nonlocal refresh_call_count
        with refresh_lock:
            refresh_call_count += 1
        # Simulate the token store being updated after refresh
        mock_store.load.return_value = fresh_token
        return True

    mock_store = MagicMock()
    # First load returns expired, after refresh returns fresh
    mock_store.load.return_value = expired_token

    with (
        patch("trust5.core.auth.token_store.TokenStore", return_value=mock_store),
        patch.object(llm, "_try_refresh_token_locked", side_effect=mock_refresh_locked),
    ):
        barrier = threading.Barrier(10, timeout=5)
        errors: list[Exception] = []

        def worker() -> None:
            try:
                barrier.wait()
                llm._ensure_token_fresh()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

    assert not errors, f"Threads raised errors: {errors}"
    # The lock ensures serialization. The first thread refreshes, subsequent
    # threads reload and see the fresh token (expires_at > margin).
    # Depending on timing, we may get 1 or a few calls, but critically
    # NOT 10 concurrent calls. Allow up to 3 for thread scheduling variance.
    assert refresh_call_count <= 3, (
        f"Expected at most a few refresh calls due to lock serialization, got {refresh_call_count}"
    )


# ---------------------------------------------------------------------------
# 8. test_post_401_refreshes_and_retries
# ---------------------------------------------------------------------------


@patch(_EMIT_PATCH)
def test_post_401_refreshes_and_retries(mock_emit: MagicMock) -> None:
    """On 401, _post() refreshes the token and retries. Second call returns 200."""
    llm = _make_llm(
        provider_name="google",
        auth_header="Authorization",
        auth_token="initial_token",
    )

    # Build two mock responses: first 401, then 200
    resp_401 = MagicMock()
    resp_401.status_code = 401

    resp_200 = MagicMock()
    resp_200.status_code = 200

    call_count = 0

    def mock_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return resp_401
        return resp_200

    with (
        patch.object(llm._session, "post", side_effect=mock_post),
        patch.object(llm, "_try_refresh_token", return_value=True),
        patch.object(llm, "_ensure_token_fresh"),
    ):
        result = llm._post("http://example.com/api", {"test": True}, "test-model", 300)

    assert result.status_code == 200
    assert call_count == 2


# ---------------------------------------------------------------------------
# 9. test_google_refresh_captures_rotated_token
# ---------------------------------------------------------------------------


def test_google_refresh_captures_rotated_token() -> None:
    """GoogleProvider.refresh() captures a rotated refresh_token from the response."""
    from trust5.core.auth.google import GoogleProvider

    provider = GoogleProvider()

    old_token = TokenData(
        access_token="old_at",
        refresh_token="old_rt",
        expires_at=time.time() + 3600,
        extra={"client_id": "cid", "client_secret": "csec"},
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "new_at",
        "refresh_token": "new_rt",
        "expires_in": 3600,
    }
    mock_resp.raise_for_status = MagicMock()

    with patch("trust5.core.auth.google.requests.post", return_value=mock_resp):
        result = provider.refresh(old_token)

    assert result.access_token == "new_at"
    assert result.refresh_token == "new_rt"


# ---------------------------------------------------------------------------
# 10. test_google_refresh_keeps_old_when_not_rotated
# ---------------------------------------------------------------------------


def test_google_refresh_keeps_old_when_not_rotated() -> None:
    """GoogleProvider.refresh() keeps old refresh_token when response omits it."""
    from trust5.core.auth.google import GoogleProvider

    provider = GoogleProvider()

    old_token = TokenData(
        access_token="old_at",
        refresh_token="old_rt",
        expires_at=time.time() + 3600,
        extra={"client_id": "cid", "client_secret": "csec"},
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "new_at",
        "expires_in": 3600,
        # NOTE: no "refresh_token" key
    }
    mock_resp.raise_for_status = MagicMock()

    with patch("trust5.core.auth.google.requests.post", return_value=mock_resp):
        result = provider.refresh(old_token)

    assert result.access_token == "new_at"
    assert result.refresh_token == "old_rt"  # kept from old token


# ---------------------------------------------------------------------------
# 11. test_agent_task_auth_error_raises_transient
# ---------------------------------------------------------------------------


def test_agent_task_auth_error_raises_transient() -> None:
    """AgentTask converts is_auth_error LLMError into a TransientError with retry_after=120."""
    from stabilize.errors import TransientError

    from trust5.core.agent_task import AgentTask

    task = AgentTask()

    # Build a minimal StageExecution mock
    stage = MagicMock()
    stage.context = {
        "agent_name": "test-agent",
        "prompt_file": "prompts/test.md",
        "user_input": "hello",
        "non_interactive": True,
    }

    # The Agent.run() should raise LLMError with auth class
    mock_agent_instance = MagicMock()
    mock_agent_instance.run.side_effect = LLMError(
        "Auth failed: 401 Unauthorized",
        retryable=True,
        error_class="auth",
    )

    with (
        patch("trust5.core.agent_task.emit"),
        patch("trust5.core.agent_task.build_project_context", return_value=""),
        patch("trust5.core.agent_task.LLM.for_tier", return_value=MagicMock()),
        patch("trust5.core.agent_task.mcp_clients") as mock_mcp_ctx,
        patch("trust5.core.agent_task.Agent", return_value=mock_agent_instance),
    ):
        # mcp_clients() is a context manager
        mock_mcp_ctx.return_value.__enter__ = MagicMock(return_value=[])
        mock_mcp_ctx.return_value.__exit__ = MagicMock(return_value=False)

        # AgentTask._load_system_prompt must return a valid prompt
        with patch.object(task, "_load_system_prompt", return_value="You are a test agent."):
            with pytest.raises(TransientError) as exc_info:
                task.execute(stage)

            assert exc_info.value.retry_after == 120.0
            assert "Auth failed" in str(exc_info.value)
