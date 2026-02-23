"""Tests for WatchdogTask in trust5/tasks/watchdog_task.py."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

from trust5.tasks.watchdog_task import _DOUBLE_EXT_RE, _GARBLED_RE, _LEGIT_DOUBLE_EXT, WatchdogTask


def _make_watchdog() -> WatchdogTask:
    return WatchdogTask()


# ── Garbled files ────────────────────────────────────────────────────


@patch("trust5.tasks.watchdog_task.emit")
def test_check_garbled_files_detects_equals_files(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "=3.0.0"), "w").close()
        open(os.path.join(tmpdir, "=1.2"), "w").close()
        w, e = _make_watchdog()._check_garbled_files(tmpdir)
        assert e > 0


@patch("trust5.tasks.watchdog_task.emit")
def test_check_garbled_files_clean(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "main.py"), "w").close()
        w, e = _make_watchdog()._check_garbled_files(tmpdir)
        assert (w, e) == (0, 0)


# ── Manifest files ──────────────────────────────────────────────────


@patch("trust5.tasks.watchdog_task.emit")
def test_check_manifest_missing(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        profile = {"required_project_files": ("pyproject.toml",)}
        w, e = _make_watchdog()._check_manifest_files(tmpdir, profile)
        assert w > 0


@patch("trust5.tasks.watchdog_task.emit")
def test_check_manifest_present(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "pyproject.toml"), "w").close()
        profile = {"required_project_files": ("pyproject.toml",)}
        w, e = _make_watchdog()._check_manifest_files(tmpdir, profile)
        assert (w, e) == (0, 0)


# ── Corrupted extensions ────────────────────────────────────────────


@patch("trust5.tasks.watchdog_task.emit")
def test_check_corrupted_double_ext(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "config.toml.py"), "w").close()
        w, e = _make_watchdog()._check_corrupted_extensions(tmpdir)
        assert w > 0


@patch("trust5.tasks.watchdog_task.emit")
def test_check_legit_double_ext_ignored(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "app.test.ts"), "w").close()
        w, e = _make_watchdog()._check_corrupted_extensions(tmpdir)
        assert (w, e) == (0, 0)


@patch("trust5.tasks.watchdog_task.emit")
def test_check_normal_ext_clean(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "main.py"), "w").close()
        w, e = _make_watchdog()._check_corrupted_extensions(tmpdir)
        assert (w, e) == (0, 0)


# ── Empty source files ──────────────────────────────────────────────


@patch("trust5.tasks.watchdog_task.emit")
def test_check_empty_source_file(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "main.py"), "w").close()
        w, e = _make_watchdog()._check_empty_source_files(tmpdir)
        assert w > 0


@patch("trust5.tasks.watchdog_task.emit")
def test_check_empty_init_py_ok(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "__init__.py"), "w").close()
        w, e = _make_watchdog()._check_empty_source_files(tmpdir)
        assert (w, e) == (0, 0)


@patch("trust5.tasks.watchdog_task.emit")
def test_check_non_empty_file_ok(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "main.py"), "w") as f:
            f.write("print('hello')\n")
        w, e = _make_watchdog()._check_empty_source_files(tmpdir)
        assert (w, e) == (0, 0)


# ── Stub files ──────────────────────────────────────────────────────


@patch("trust5.tasks.watchdog_task.emit")
def test_check_stub_file_detected(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "engine.py"), "w") as f:
            f.write("# implementation required\n")
        w, e = _make_watchdog()._check_stub_files(tmpdir)
        assert w > 0


@patch("trust5.tasks.watchdog_task.emit")
def test_check_real_file_ok(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "engine.py"), "w") as f:
            f.write(
                "def compute(x, y):\n    return x + y\n\ndef process(data):\n    return [compute(d, 1) for d in data]\n"
            )
        w, e = _make_watchdog()._check_stub_files(tmpdir)
        assert (w, e) == (0, 0)


# ── Regex patterns ──────────────────────────────────────────────────


def test_garbled_re_matches():
    assert _GARBLED_RE.match("=3.0.0")


def test_garbled_re_no_match():
    assert not _GARBLED_RE.match("main.py")


def test_double_ext_re_matches():
    assert _DOUBLE_EXT_RE.search("config.toml.py")


def test_double_ext_legit():
    assert ".test.ts" in _LEGIT_DOUBLE_EXT
