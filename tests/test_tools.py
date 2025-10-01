import os
import re
from unittest.mock import MagicMock, patch
import pytest
from trust5.core.tools import _BLOCKED_COMMAND_PATTERNS, _VALID_PACKAGE_RE, Tools, _matches_test_pattern

def _suppress_emit():
    """Prevent real event emission during tests."""
    with (
        patch("trust5.core.tools.emit") as mock_emit,
        patch("trust5.core.tools.emit_block") as mock_emit_block,
    ):
        yield mock_emit, mock_emit_block

def tools():
    """Unconstrained Tools instance (no owned_files restriction)."""
    return Tools()

def _reset_non_interactive():
    """Ensure the class-level _non_interactive flag is reset between tests."""
    original = Tools._non_interactive
    yield
    Tools._non_interactive = original

def test_run_bash_blocks_rm_rf(tools: Tools):
    result = tools.run_bash("rm -rf /")
    assert "blocked by safety filter" in result.lower() or "blocked" in result.lower()

def test_run_bash_blocks_rm_rf_variant(tools: Tools):
    """rm -fr (reversed flags) should also be blocked."""
    result = tools.run_bash("rm -fr /tmp/important")
    assert "blocked" in result.lower()

def test_run_bash_blocks_mkfs(tools: Tools):
    result = tools.run_bash("mkfs.ext4 /dev/sda1")
    assert "blocked" in result.lower()

def test_run_bash_blocks_dd(tools: Tools):
    result = tools.run_bash("dd if=/dev/zero of=/dev/sda bs=1M")
    assert "blocked" in result.lower()

def test_run_bash_blocks_chmod_777(tools: Tools):
    result = tools.run_bash("chmod 777 /etc/passwd")
    assert "blocked" in result.lower()

def test_run_bash_blocks_chmod_recursive_777(tools: Tools):
    result = tools.run_bash("chmod -R 777 /var")
    assert "blocked" in result.lower()
