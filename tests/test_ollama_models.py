"""Tests for trust5/core/ollama_models.py — Ollama model availability and pulling."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest
import requests

from trust5.core.message import M
from trust5.core.ollama_models import (
    _get_required_models,
    _is_cloud_model,
    _list_available_models,
    _normalize_model_name,
    _ollama_available,
    _ollama_server_running,
    _pull_model,
    ensure_ollama_models,
)

# ── _is_cloud_model ──────────────────────────────────────────────────────


def test_is_cloud_model_with_cloud_tag():
    assert _is_cloud_model("qwen3-coder-next:cloud") is True


def test_is_cloud_model_without_cloud_tag():
    assert _is_cloud_model("llama3:latest") is False


def test_is_cloud_model_bare_name():
    assert _is_cloud_model("llama3") is False


# ── _normalize_model_name ────────────────────────────────────────────────


def test_normalize_bare_name_adds_latest():
    assert _normalize_model_name("llama3") == "llama3:latest"


def test_normalize_tagged_name_unchanged():
    assert _normalize_model_name("qwen3:cloud") == "qwen3:cloud"


def test_normalize_latest_tag_unchanged():
    assert _normalize_model_name("llama3:latest") == "llama3:latest"


# ── _ollama_available ────────────────────────────────────────────────────


@patch("trust5.core.ollama_models.shutil.which", return_value="/usr/local/bin/ollama")
def test_ollama_available_found(mock_which):
    assert _ollama_available() is True
    mock_which.assert_called_once_with("ollama")


@patch("trust5.core.ollama_models.shutil.which", return_value=None)
def test_ollama_available_not_found(mock_which):
    assert _ollama_available() is False


# ── _ollama_server_running ───────────────────────────────────────────────


@patch("trust5.core.ollama_models.requests.get")
def test_server_running_ok(mock_get):
    mock_get.return_value = MagicMock(status_code=200)
    assert _ollama_server_running() is True


@patch("trust5.core.ollama_models.requests.get", side_effect=requests.ConnectionError)
def test_server_running_connection_error(mock_get):
    assert _ollama_server_running() is False


@patch("trust5.core.ollama_models.requests.get", side_effect=requests.Timeout)
def test_server_running_timeout(mock_get):
    assert _ollama_server_running() is False


# ── _list_available_models ───────────────────────────────────────────────


@patch("trust5.core.ollama_models.requests.get")
def test_list_models_parses_response(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "models": [
            {"name": "llama3:latest"},
            {"name": "qwen3:cloud"},
        ]
    }
    mock_get.return_value = mock_resp
    result = _list_available_models()
    assert result == {"llama3:latest", "qwen3:cloud"}


@patch("trust5.core.ollama_models.requests.get")
def test_list_models_normalizes_names(mock_get):
    """Bare model names in server response get :latest appended."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"models": [{"name": "llama3"}]}
    mock_get.return_value = mock_resp
    result = _list_available_models()
    assert "llama3:latest" in result


@patch("trust5.core.ollama_models.requests.get", side_effect=requests.ConnectionError)
def test_list_models_returns_empty_on_error(mock_get):
    assert _list_available_models() == set()


@patch("trust5.core.ollama_models.requests.get")
def test_list_models_empty_server(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"models": []}
    mock_get.return_value = mock_resp
    assert _list_available_models() == set()


# ── _get_required_models ─────────────────────────────────────────────────


@patch("trust5.core.ollama_models.load_global_config")
def test_get_required_models_deduplicates(mock_cfg):
    mock_ollama = MagicMock()
    mock_ollama.best = "model-a:cloud"
    mock_ollama.good = "model-b:cloud"
    mock_ollama.fast = "model-a:cloud"  # duplicate of best
    mock_ollama.watchdog = "model-c:cloud"
    mock_ollama.default = "model-b:cloud"  # duplicate of good
    mock_ollama.fallback_chain = ["model-a:cloud", "model-d:cloud"]
    mock_cfg.return_value.models.ollama = mock_ollama
    result = _get_required_models()
    assert result == {"model-a:cloud", "model-b:cloud", "model-c:cloud", "model-d:cloud"}


@patch("trust5.core.ollama_models.load_global_config")
def test_get_required_models_normalizes_bare_names(mock_cfg):
    mock_ollama = MagicMock()
    mock_ollama.best = "llama3"  # no tag
    mock_ollama.good = "llama3"
    mock_ollama.fast = "llama3"
    mock_ollama.watchdog = "llama3"
    mock_ollama.default = "llama3"
    mock_ollama.fallback_chain = []
    mock_cfg.return_value.models.ollama = mock_ollama
    result = _get_required_models()
    assert result == {"llama3:latest"}


# ── _pull_model ──────────────────────────────────────────────────────────


@patch("trust5.core.ollama_models.subprocess.run")
def test_pull_model_success(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    assert _pull_model("qwen3:cloud") is True
    mock_run.assert_called_once()
    args = mock_run.call_args
    assert args[0][0] == ["ollama", "pull", "qwen3:cloud"]


@patch("trust5.core.ollama_models.subprocess.run")
def test_pull_model_failure(mock_run):
    mock_run.return_value = MagicMock(returncode=1, stderr="not found")
    assert _pull_model("bad:model") is False


@patch("trust5.core.ollama_models.subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 60))
def test_pull_model_timeout(mock_run):
    assert _pull_model("huge:local") is False


@patch("trust5.core.ollama_models.subprocess.run", side_effect=OSError("spawn failed"))
def test_pull_model_os_error(mock_run):
    assert _pull_model("model:tag") is False


@patch("trust5.core.ollama_models.subprocess.run")
def test_pull_cloud_model_uses_short_timeout(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    _pull_model("gemini-3-flash:cloud")
    args = mock_run.call_args
    assert args[1]["timeout"] == 120  # OLLAMA_PULL_TIMEOUT_CLOUD


@patch("trust5.core.ollama_models.subprocess.run")
def test_pull_local_model_uses_long_timeout(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    _pull_model("llama3:latest")
    args = mock_run.call_args
    assert args[1]["timeout"] == 600  # OLLAMA_PULL_TIMEOUT_LOCAL


# ── ensure_ollama_models ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_models_checked():
    """Reset the idempotency guard before each test."""
    import trust5.core.ollama_models as mod

    mod._models_checked = False
    yield
    mod._models_checked = False


@patch("trust5.core.ollama_models.get_active_token", return_value=("provider", "token"))
@patch("trust5.core.ollama_models.emit")
def test_ensure_skips_non_ollama(mock_emit, mock_token):
    """When get_active_token returns a provider, do nothing."""
    ensure_ollama_models()
    for call in mock_emit.call_args_list:
        msg = call[0][1] if len(call[0]) > 1 else ""
        assert "Ollama" not in msg and "ollama" not in msg


@patch("trust5.core.ollama_models.get_active_token", return_value=None)
@patch("trust5.core.ollama_models._ollama_available", return_value=False)
@patch("trust5.core.ollama_models.emit")
def test_ensure_warns_when_cli_missing(mock_emit, mock_avail, mock_token):
    ensure_ollama_models()
    warn_msgs = [c[0][1] for c in mock_emit.call_args_list if c[0][0] == M.SWRN]
    assert any("not found" in m for m in warn_msgs)


@patch("trust5.core.ollama_models.get_active_token", return_value=None)
@patch("trust5.core.ollama_models._ollama_available", return_value=True)
@patch("trust5.core.ollama_models._ollama_server_running", return_value=False)
@patch("trust5.core.ollama_models.emit")
def test_ensure_warns_when_server_down(mock_emit, mock_server, mock_avail, mock_token):
    ensure_ollama_models()
    warn_msgs = [c[0][1] for c in mock_emit.call_args_list if c[0][0] == M.SWRN]
    assert any("not responding" in m for m in warn_msgs)


@patch("trust5.core.ollama_models.get_active_token", return_value=None)
@patch("trust5.core.ollama_models._ollama_available", return_value=True)
@patch("trust5.core.ollama_models._ollama_server_running", return_value=True)
@patch("trust5.core.ollama_models._get_required_models", return_value={"model-a:cloud", "model-b:cloud"})
@patch("trust5.core.ollama_models._list_available_models", return_value={"model-a:cloud", "model-b:cloud"})
@patch("trust5.core.ollama_models._pull_model")
@patch("trust5.core.ollama_models.emit")
def test_ensure_all_present_skips_pull(mock_emit, mock_pull, mock_list, mock_req, mock_server, mock_avail, mock_token):
    """When all models are available, no pull calls are made."""
    ensure_ollama_models()
    mock_pull.assert_not_called()
    info_msgs = [c[0][1] for c in mock_emit.call_args_list if c[0][0] == M.SINF]
    assert any("All" in m and "available" in m for m in info_msgs)


@patch("trust5.core.ollama_models.get_active_token", return_value=None)
@patch("trust5.core.ollama_models._ollama_available", return_value=True)
@patch("trust5.core.ollama_models._ollama_server_running", return_value=True)
@patch("trust5.core.ollama_models._get_required_models", return_value={"model-a:cloud", "model-b:cloud"})
@patch("trust5.core.ollama_models._list_available_models", return_value={"model-a:cloud"})
@patch("trust5.core.ollama_models._pull_model", return_value=True)
@patch("trust5.core.ollama_models.emit")
def test_ensure_pulls_only_missing(mock_emit, mock_pull, mock_list, mock_req, mock_server, mock_avail, mock_token):
    """Only missing models are pulled."""
    ensure_ollama_models()
    mock_pull.assert_called_once_with("model-b:cloud")


@patch("trust5.core.ollama_models.get_active_token", return_value=None)
@patch("trust5.core.ollama_models._ollama_available", return_value=True)
@patch("trust5.core.ollama_models._ollama_server_running", return_value=True)
@patch("trust5.core.ollama_models._get_required_models", return_value={"model-a:cloud"})
@patch("trust5.core.ollama_models._list_available_models", return_value=set())
@patch("trust5.core.ollama_models._pull_model", return_value=False)
@patch("trust5.core.ollama_models.emit")
def test_ensure_reports_pull_failure(mock_emit, mock_pull, mock_list, mock_req, mock_server, mock_avail, mock_token):
    """Failed pulls are reported as warnings, pipeline is not blocked."""
    ensure_ollama_models()  # Should NOT raise
    warn_msgs = [c[0][1] for c in mock_emit.call_args_list if c[0][0] == M.SWRN]
    assert any("FAILED" in m or "Failed" in m for m in warn_msgs)


@patch("trust5.core.ollama_models.get_active_token", return_value=None)
@patch("trust5.core.ollama_models._ollama_available", return_value=True)
@patch("trust5.core.ollama_models._ollama_server_running", return_value=True)
@patch("trust5.core.ollama_models._get_required_models", return_value={"m:cloud"})
@patch("trust5.core.ollama_models._list_available_models", return_value=set())
@patch("trust5.core.ollama_models._pull_model", return_value=True)
@patch("trust5.core.ollama_models.emit")
def test_ensure_idempotent(mock_emit, mock_pull, mock_list, mock_req, mock_server, mock_avail, mock_token):
    """Second call is a no-op."""
    ensure_ollama_models()
    first_call_count = mock_pull.call_count
    assert first_call_count == 1

    ensure_ollama_models()  # second call
    assert mock_pull.call_count == first_call_count  # no additional pulls
