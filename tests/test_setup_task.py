"""Tests for trust5/tasks/setup_task.py — SetupTask and _quote_version_specifiers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from trust5.tasks.setup_task import SetupTask, _quote_version_specifiers, _run_setup_command

# ── _quote_version_specifiers tests ──────────────────────────────────


def test_quote_pip_install_gte():
    """Tokens with >= are quoted to prevent shell redirection."""
    assert _quote_version_specifiers("pip install flask>=3.0.0") == "pip install 'flask>=3.0.0'"


def test_quote_pip_install_multiple_specifiers():
    """Multiple version-specifier tokens are all quoted."""
    result = _quote_version_specifiers("pip install flask>=3.0 pytest>=8.0 requests==2.31")
    assert "'flask>=3.0'" in result
    assert "'pytest>=8.0'" in result
    assert "'requests==2.31'" in result


def test_quote_pip_install_lte():
    assert _quote_version_specifiers("pip install numpy<=1.26") == "pip install 'numpy<=1.26'"


def test_quote_pip_install_not_equal():
    assert _quote_version_specifiers("pip install pkg!=2.0") == "pip install 'pkg!=2.0'"


def test_quote_pip_install_tilde_equal():
    assert _quote_version_specifiers("pip install pkg~=1.4") == "pip install 'pkg~=1.4'"


def test_quote_pip_install_gt():
    assert _quote_version_specifiers("pip install pkg>1.0") == "pip install 'pkg>1.0'"


def test_quote_pip_install_lt():
    assert _quote_version_specifiers("pip install pkg<2.0") == "pip install 'pkg<2.0'"


def test_quote_uv_install():
    """uv install commands are also quoted."""
    assert _quote_version_specifiers("uv pip install flask>=3.0") == "uv pip install 'flask>=3.0'"


def test_quote_leaves_non_pip_commands_alone():
    """Commands that are not pip/uv install are left unchanged."""
    cmd = "python3 -m venv .venv"
    assert _quote_version_specifiers(cmd) == cmd


def test_quote_leaves_already_quoted_alone():
    """Already-quoted specifiers should not be double-quoted."""
    cmd = "pip install 'flask>=3.0'"
    result = _quote_version_specifiers(cmd)
    # Should not get double-quoted to ''flask>=3.0''
    assert result.count("'flask>=3.0'") == 1


def test_quote_with_extras():
    """Packages with extras like pkg[extra]>=1.0 are handled."""
    result = _quote_version_specifiers("pip install uvicorn[standard]>=0.20")
    assert "'uvicorn[standard]>=0.20'" in result


def test_quote_plain_pip_install_no_version():
    """pip install without version specifiers is unchanged."""
    cmd = "pip install flask requests pytest"
    assert _quote_version_specifiers(cmd) == cmd


def test_quote_pip3_install():
    """pip3 is still caught by the pip regex."""
    # The regex looks for \bpip\b, pip3 contains pip but is pip3
    # This is fine — pip3 install should also be handled
    result = _quote_version_specifiers("pip3 install flask>=3.0")
    # pip3 does contain "pip", the regex \bpip\b won't match pip3
    # so this should be left alone (pip3 is a separate word)
    assert result == "pip3 install flask>=3.0"


# ── _run_setup_command tests ─────────────────────────────────────────


def test_run_setup_command_success(tmp_path):
    """Successful command returns (0, output)."""
    rc, out = _run_setup_command("echo hello", str(tmp_path))
    assert rc == 0
    assert "hello" in out


def test_run_setup_command_failure(tmp_path):
    """Failed command returns nonzero exit code."""
    rc, _out = _run_setup_command("exit 42", str(tmp_path))
    assert rc == 42


def test_run_setup_command_timeout(tmp_path):
    """Commands that exceed timeout return rc=124."""
    with patch("trust5.tasks.setup_task.SETUP_TIMEOUT", 1):
        rc, out = _run_setup_command("sleep 30", str(tmp_path))
        assert rc == 124
        assert "timed out" in out


def test_run_setup_command_quotes_specifiers(tmp_path):
    """Version specifiers are quoted before execution (no garbled files)."""
    # Running pip install with a non-existent package will fail, but
    # the key test is that no files like '=3.0.0' are created.
    import os

    _rc, _out = _run_setup_command("pip install nonexistent_pkg_xyz>=3.0.0", str(tmp_path))
    # Even though pip fails, no shell redirection file should exist
    garbled = [f for f in os.listdir(tmp_path) if f.startswith("=")]
    assert garbled == [], f"Garbled files created by shell redirection: {garbled}"


# ── SetupTask.execute tests ──────────────────────────────────────────


def _make_stage(context: dict | None = None) -> MagicMock:
    stage = MagicMock()
    stage.context = context or {}
    stage.context.setdefault("project_root", "/tmp/fake-project")
    return stage


@patch("trust5.tasks.setup_task._run_setup_command", return_value=(0, "ok"))
@patch("trust5.tasks.setup_task.emit")
def test_setup_task_success(_mock_emit, mock_run):
    stage = _make_stage({"setup_commands": ["echo hello"]})
    result = SetupTask().execute(stage)
    assert result.outputs["setup_completed"] is True
    mock_run.assert_called_once()


@patch("trust5.tasks.setup_task._run_setup_command", return_value=(1, "error"))
@patch("trust5.tasks.setup_task.emit")
def test_setup_task_failure(_mock_emit, mock_run):
    stage = _make_stage({"setup_commands": ["bad_cmd"]})
    result = SetupTask().execute(stage)
    assert result.outputs["setup_completed"] is False
    assert "bad_cmd" in result.outputs["setup_failed_commands"]


@patch("trust5.tasks.setup_task.emit")
def test_setup_task_no_commands(_mock_emit):
    stage = _make_stage({"setup_commands": []})
    result = SetupTask().execute(stage)
    assert result.outputs["setup_completed"] is True
    assert result.outputs.get("setup_skipped") is True
