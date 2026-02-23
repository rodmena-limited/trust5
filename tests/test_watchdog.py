"""Tests for WatchdogTask in trust5/tasks/watchdog_task.py."""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch

from trust5.tasks.watchdog_task import (
    _DOUBLE_EXT_RE,
    _GARBLED_RE,
    _LEGIT_DOUBLE_EXT,
    WatchdogTask,
    load_watchdog_findings,
    signal_pipeline_done,
)


def _make_watchdog() -> WatchdogTask:
    return WatchdogTask()


# ── Garbled files ────────────────────────────────────────────────────


@patch("trust5.tasks.watchdog_task.emit")
def test_check_garbled_files_detects_equals_files(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "=3.0.0"), "w").close()
        open(os.path.join(tmpdir, "=1.2"), "w").close()
        findings: list[dict[str, str]] = []
        w, e = _make_watchdog()._check_garbled_files(tmpdir, findings)
        assert e > 0
        assert len(findings) >= 2
        assert all(f["severity"] == "error" for f in findings)
        assert all(f["category"] == "garbled_file" for f in findings)


@patch("trust5.tasks.watchdog_task.emit")
def test_check_garbled_files_clean(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "main.py"), "w").close()
        findings: list[dict[str, str]] = []
        w, e = _make_watchdog()._check_garbled_files(tmpdir, findings)
        assert (w, e) == (0, 0)
        assert findings == []


# ── Manifest files ──────────────────────────────────────────────────


@patch("trust5.tasks.watchdog_task.emit")
def test_check_manifest_missing(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        profile = {"required_project_files": ("pyproject.toml",)}
        findings: list[dict[str, str]] = []
        w, e = _make_watchdog()._check_manifest_files(tmpdir, profile, findings)
        assert w > 0
        assert len(findings) == 1
        assert findings[0]["category"] == "missing_manifest"
        assert findings[0]["file"] == "pyproject.toml"


@patch("trust5.tasks.watchdog_task.emit")
def test_check_manifest_present(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "pyproject.toml"), "w").close()
        profile = {"required_project_files": ("pyproject.toml",)}
        findings: list[dict[str, str]] = []
        w, e = _make_watchdog()._check_manifest_files(tmpdir, profile, findings)
        assert (w, e) == (0, 0)
        assert findings == []


# ── Corrupted extensions ────────────────────────────────────────────


@patch("trust5.tasks.watchdog_task.emit")
def test_check_corrupted_double_ext(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "config.toml.py"), "w").close()
        findings: list[dict[str, str]] = []
        w, e = _make_watchdog()._check_corrupted_extensions(tmpdir, findings)
        assert w > 0
        assert len(findings) == 1
        assert findings[0]["category"] == "corrupted_extension"


@patch("trust5.tasks.watchdog_task.emit")
def test_check_legit_double_ext_ignored(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "app.test.ts"), "w").close()
        findings: list[dict[str, str]] = []
        w, e = _make_watchdog()._check_corrupted_extensions(tmpdir, findings)
        assert (w, e) == (0, 0)
        assert findings == []


@patch("trust5.tasks.watchdog_task.emit")
def test_check_normal_ext_clean(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "main.py"), "w").close()
        findings: list[dict[str, str]] = []
        w, e = _make_watchdog()._check_corrupted_extensions(tmpdir, findings)
        assert (w, e) == (0, 0)
        assert findings == []


# ── Empty source files ──────────────────────────────────────────────


@patch("trust5.tasks.watchdog_task.emit")
def test_check_empty_source_file(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "main.py"), "w").close()
        findings: list[dict[str, str]] = []
        w, e = _make_watchdog()._check_empty_source_files(tmpdir, findings)
        assert w > 0
        assert len(findings) == 1
        assert findings[0]["category"] == "empty_source"


@patch("trust5.tasks.watchdog_task.emit")
def test_check_empty_init_py_ok(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "__init__.py"), "w").close()
        findings: list[dict[str, str]] = []
        w, e = _make_watchdog()._check_empty_source_files(tmpdir, findings)
        assert (w, e) == (0, 0)
        assert findings == []


@patch("trust5.tasks.watchdog_task.emit")
def test_check_non_empty_file_ok(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "main.py"), "w") as f:
            f.write("print('hello')\n")
        findings: list[dict[str, str]] = []
        w, e = _make_watchdog()._check_empty_source_files(tmpdir, findings)
        assert (w, e) == (0, 0)
        assert findings == []


# ── Stub files ──────────────────────────────────────────────────────


@patch("trust5.tasks.watchdog_task.emit")
def test_check_stub_file_detected(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "engine.py"), "w") as f:
            f.write("# implementation required\n")
        findings: list[dict[str, str]] = []
        w, e = _make_watchdog()._check_stub_files(tmpdir, findings)
        assert w > 0
        assert len(findings) == 1
        assert findings[0]["category"] == "stub_file"


@patch("trust5.tasks.watchdog_task.emit")
def test_check_real_file_ok(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "engine.py"), "w") as f:
            f.write(
                "def compute(x, y):\n    return x + y\n\ndef process(data):\n    return [compute(d, 1) for d in data]\n"
            )
        findings: list[dict[str, str]] = []
        w, e = _make_watchdog()._check_stub_files(tmpdir, findings)
        assert (w, e) == (0, 0)
        assert findings == []


# ── Regex patterns ──────────────────────────────────────────────────


def test_garbled_re_matches():
    assert _GARBLED_RE.match("=3.0.0")


def test_garbled_re_no_match():
    assert not _GARBLED_RE.match("main.py")


def test_double_ext_re_matches():
    assert _DOUBLE_EXT_RE.search("config.toml.py")


def test_double_ext_legit():
    assert ".test.ts" in _LEGIT_DOUBLE_EXT


# ── _write_report ───────────────────────────────────────────────────


def test_write_report_creates_valid_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        findings = [
            {"severity": "error", "category": "garbled_file", "file": "=3.0", "message": "Garbled file"},
            {"severity": "warning", "category": "stub_file", "file": "main.py", "message": "Stub detected"},
        ]
        WatchdogTask._write_report(tmpdir, findings, 7)
        report_path = os.path.join(tmpdir, ".trust5", "watchdog_report.json")
        assert os.path.exists(report_path)
        with open(report_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["check_number"] == 7
        assert len(data["findings"]) == 2
        assert data["findings"][0]["severity"] == "error"
        assert data["findings"][1]["category"] == "stub_file"


def test_write_report_creates_trust5_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        WatchdogTask._write_report(tmpdir, [], 1)
        assert os.path.isdir(os.path.join(tmpdir, ".trust5"))


def test_write_report_empty_findings():
    with tempfile.TemporaryDirectory() as tmpdir:
        WatchdogTask._write_report(tmpdir, [], 3)
        report_path = os.path.join(tmpdir, ".trust5", "watchdog_report.json")
        with open(report_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["findings"] == []
        assert data["check_number"] == 3


# ── load_watchdog_findings ──────────────────────────────────────────


def test_load_watchdog_findings_no_report():
    """Returns empty string when no report file exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = load_watchdog_findings(tmpdir)
        assert result == ""


def test_load_watchdog_findings_empty_findings():
    """Returns empty string when report has no findings."""
    with tempfile.TemporaryDirectory() as tmpdir:
        WatchdogTask._write_report(tmpdir, [], 5)
        result = load_watchdog_findings(tmpdir)
        assert result == ""


def test_load_watchdog_findings_with_findings():
    """Returns formatted markdown when findings exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        findings = [
            {"severity": "error", "category": "garbled_file", "file": "=3.0", "message": "Garbled file detected"},
            {"severity": "warning", "category": "missing_manifest", "file": "pyproject.toml", "message": "Missing"},
        ]
        WatchdogTask._write_report(tmpdir, findings, 2)
        result = load_watchdog_findings(tmpdir)
        assert "## Watchdog Audit Findings" in result
        assert "**[ERROR]**" in result
        assert "**[WARNING]**" in result
        assert "`=3.0`" in result
        assert "`pyproject.toml`" in result
        assert "(garbled_file)" in result
        assert "(missing_manifest)" in result


def test_load_watchdog_findings_corrupt_json():
    """Returns empty string when report JSON is corrupted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        trust5_dir = os.path.join(tmpdir, ".trust5")
        os.makedirs(trust5_dir)
        with open(os.path.join(trust5_dir, "watchdog_report.json"), "w") as f:
            f.write("{not valid json")
        result = load_watchdog_findings(tmpdir)
        assert result == ""


# ── _run_checks integration ─────────────────────────────────────────


@patch("trust5.tasks.watchdog_task.emit")
def test_run_checks_populates_findings(_mock_emit):
    """Verify _run_checks aggregates findings from all sub-checks."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a garbled file and stub file
        open(os.path.join(tmpdir, "=2.0"), "w").close()
        with open(os.path.join(tmpdir, "module.py"), "w") as f:
            f.write("# implementation required\n")
        profile = {"required_project_files": ("Cargo.toml",)}
        context: dict = {}
        findings: list[dict[str, str]] = []
        w, e = _make_watchdog()._run_checks(tmpdir, profile, context, findings)
        assert e >= 1  # garbled file
        assert w >= 2  # missing manifest + stub
        categories = {f["category"] for f in findings}
        assert "garbled_file" in categories
        assert "missing_manifest" in categories
        assert "stub_file" in categories


# ── _emit_findings_block ─────────────────────────────────────────────


@patch("trust5.tasks.watchdog_task.emit_block")
def test_emit_findings_block_warning_only(mock_emit_block):
    """Emit block with WDWN code when all findings are warnings."""
    findings = [
        {"severity": "warning", "category": "stub_file", "file": "main.py", "message": "Stub detected"},
    ]
    WatchdogTask._emit_findings_block(findings, 3)
    mock_emit_block.assert_called_once()
    args = mock_emit_block.call_args
    from trust5.core.message import M as _M
    assert args[0][0] == _M.WDWN
    assert "Watchdog Audit (check #3)" in args[0][1]
    assert "stub_file" in args[0][2]
    assert "\u26a0\ufe0f" in args[0][2]  # warning icon


@patch("trust5.tasks.watchdog_task.emit_block")
def test_emit_findings_block_error_escalates(mock_emit_block):
    """Emit block with WDER code when any finding is an error."""
    findings = [
        {"severity": "warning", "category": "stub_file", "file": "main.py", "message": "Stub"},
        {"severity": "error", "category": "garbled_file", "file": "=3.0", "message": "Garbled"},
    ]
    WatchdogTask._emit_findings_block(findings, 7)
    mock_emit_block.assert_called_once()
    args = mock_emit_block.call_args
    from trust5.core.message import M as _M
    assert args[0][0] == _M.WDER
    assert "\u274c" in args[0][2]  # error icon
    assert "\u26a0\ufe0f" in args[0][2]  # warning icon too


@patch("trust5.tasks.watchdog_task.emit_block")
def test_emit_findings_block_content_format(mock_emit_block):
    """Block content includes severity, category, file, and message."""
    findings = [
        {"severity": "error", "category": "garbled_file", "file": "=3.0", "message": "Bad file"},
    ]
    WatchdogTask._emit_findings_block(findings, 1)
    content = mock_emit_block.call_args[0][2]
    assert "[ERROR]" in content
    assert "garbled_file" in content
    assert "=3.0" in content
    assert "Bad file" in content


# ── Sentinel / pipeline completion ────────────────────────────────────


def test_signal_pipeline_done_creates_sentinel():
    """signal_pipeline_done writes the sentinel file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        signal_pipeline_done(tmpdir)
        sentinel = os.path.join(tmpdir, ".trust5", "pipeline_complete")
        assert os.path.exists(sentinel)
        content = open(sentinel).read()
        assert content  # contains a monotonic timestamp


def test_signal_pipeline_done_creates_trust5_dir():
    """signal_pipeline_done creates .trust5/ if it doesn't exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        trust5_dir = os.path.join(tmpdir, ".trust5")
        assert not os.path.exists(trust5_dir)
        signal_pipeline_done(tmpdir)
        assert os.path.isdir(trust5_dir)


def test_pipeline_done_true_when_sentinel_exists():
    """_pipeline_done returns True when sentinel file exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        signal_pipeline_done(tmpdir)
        assert WatchdogTask._pipeline_done(tmpdir) is True


def test_pipeline_done_false_when_no_sentinel():
    """_pipeline_done returns False when no sentinel exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        assert WatchdogTask._pipeline_done(tmpdir) is False


def test_clear_sentinel_removes_file():
    """_clear_sentinel removes the sentinel file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        signal_pipeline_done(tmpdir)
        assert WatchdogTask._pipeline_done(tmpdir) is True
        WatchdogTask._clear_sentinel(tmpdir)
        assert WatchdogTask._pipeline_done(tmpdir) is False


def test_clear_sentinel_noop_when_missing():
    """_clear_sentinel does not raise when sentinel doesn't exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Should not raise
        WatchdogTask._clear_sentinel(tmpdir)
