"""Tests for WatchdogTask in trust5/tasks/watchdog_task.py."""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

from trust5.tasks.watchdog_task import (
    _DOUBLE_EXT_RE,
    _GARBLED_RE,
    _LEGIT_DOUBLE_EXT,
    PipelineHealth,
    WatchdogTask,
    _build_narrative_prompt,
    _run_llm_narrative,
    _start_event_consumer,
    check_rebuild_signal,
    clear_rebuild_signal,
    load_watchdog_findings,
    signal_pipeline_done,
    signal_rebuild,
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
        assert "## Watchdog Pipeline Status" in result
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
    assert "Watchdog (check #3)" in args[0][1]
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


@patch("trust5.tasks.watchdog_task.emit_block")
def test_emit_findings_block_with_narrative(mock_emit_block):
    """Narrative appears at the top of the block, findings below."""
    findings = [
        {"severity": "warning", "category": "stub_file", "file": "main.py", "message": "Stub detected"},
    ]
    WatchdogTask._emit_findings_block(findings, 2, narrative="Pipeline is recovering.")
    content = mock_emit_block.call_args[0][2]
    assert content.startswith("Pipeline is recovering.")
    assert "stub_file" in content


@patch("trust5.tasks.watchdog_task.emit_block")
def test_emit_findings_block_narrative_only(mock_emit_block):
    """Narrative-only block (no rule findings)."""
    WatchdogTask._emit_findings_block([], 4, narrative="All systems nominal.")
    content = mock_emit_block.call_args[0][2]
    assert "All systems nominal." in content


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


# ════════════════════════════════════════════════════════════════════
# New tests for hybrid watchdog (rules, EventBus, LLM auditor)
# ════════════════════════════════════════════════════════════════════


# ── PipelineHealth ─────────────────────────────────────────────────


def test_pipeline_health_defaults():
    h = PipelineHealth()
    assert h.repair_attempts == 0
    assert h.jump_count == 0
    assert h.stages_completed == []
    assert h.stages_failed == []
    assert h.tool_calls_by_stage == {}
    assert h.consecutive_readonly_turns == 0
    assert h.llm_audit_count == 0


def test_pipeline_health_to_dict():
    h = PipelineHealth(repair_attempts=2, jump_count=5)
    h.stages_completed.append("implement")
    h.stages_failed.append("validate")
    h.tool_calls_by_stage["implement"] = 35
    d = h.to_dict()
    assert d["repair_attempts"] == 2
    assert d["jump_count"] == 5
    assert "implement" in d["stages_completed"]
    assert "validate" in d["stages_failed"]
    assert d["tool_calls_by_stage"]["implement"] == 35
    assert d["consecutive_readonly_turns"] == 0


def test_pipeline_health_mutation():
    h = PipelineHealth()
    h.repair_attempts = 3
    h.jump_count = 20
    h.consecutive_readonly_turns = 10
    assert h.repair_attempts == 3
    assert h.jump_count == 20
    assert h.consecutive_readonly_turns == 10


# ── Rule: tool_availability ─────────────────────────────────────────


def test_rule_tool_availability_present():
    profile = {"tool_check_commands": ("python3 -c 'import sys'",)}
    findings = _make_watchdog()._rule_tool_availability(profile)
    assert findings == []


def test_rule_tool_availability_missing():
    profile = {"tool_check_commands": ("nonexistent_binary_xyz123 --version",)}
    findings = _make_watchdog()._rule_tool_availability(profile)
    assert len(findings) == 1
    assert findings[0]["category"] == "tool_missing"
    assert findings[0]["severity"] == "warning"


def test_rule_tool_availability_empty():
    profile = {}
    findings = _make_watchdog()._rule_tool_availability(profile)
    assert findings == []


# ── Rule: test_discovery ──────────────────────────────────────────


def test_rule_test_discovery_before_implement():
    with tempfile.TemporaryDirectory() as tmpdir:
        health = PipelineHealth()
        profile = {"test_discovery_command": "pytest --collect-only", "extensions": (".py",)}
        findings = _make_watchdog()._rule_test_discovery(tmpdir, profile, health)
        assert findings == []


def test_rule_test_discovery_no_tests():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "main.py"), "w") as f:
            f.write("print('hello')")
        health = PipelineHealth()
        health.stages_completed.append("implement")
        profile = {"test_discovery_command": "pytest --collect-only", "extensions": (".py",)}
        findings = _make_watchdog()._rule_test_discovery(tmpdir, profile, health)
        assert len(findings) == 1
        assert findings[0]["category"] == "no_tests"


def test_rule_test_discovery_has_tests():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "test_main.py"), "w") as f:
            f.write("def test_x(): pass")
        health = PipelineHealth()
        health.stages_completed.append("implement")
        profile = {"test_discovery_command": "pytest --collect-only", "extensions": (".py",)}
        findings = _make_watchdog()._rule_test_discovery(tmpdir, profile, health)
        assert findings == []


# ── Rule: manifest_valid ──────────────────────────────────────────


def test_rule_manifest_valid_pass():
    with tempfile.TemporaryDirectory() as tmpdir:
        profile = {"manifest_validators": ("true",)}
        findings = _make_watchdog()._rule_manifest_valid(tmpdir, profile)
        assert findings == []


def test_rule_manifest_valid_fail():
    with tempfile.TemporaryDirectory() as tmpdir:
        profile = {"manifest_validators": ("false",)}
        findings = _make_watchdog()._rule_manifest_valid(tmpdir, profile)
        assert len(findings) == 1
        assert findings[0]["category"] == "manifest_invalid"
        assert findings[0]["severity"] == "error"


def test_rule_manifest_valid_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        profile = {}
        findings = _make_watchdog()._rule_manifest_valid(tmpdir, profile)
        assert findings == []


# ── Rule: repair_loop ──────────────────────────────────────────────


def test_rule_repair_loop_low():
    health = PipelineHealth(repair_attempts=1, jump_count=5)
    findings = _make_watchdog()._rule_repair_loop(health)
    assert findings == []


def test_rule_repair_loop_high_repairs():
    health = PipelineHealth(repair_attempts=3)
    findings = _make_watchdog()._rule_repair_loop(health)
    assert len(findings) == 1
    assert findings[0]["category"] == "repair_loop"
    assert findings[0]["severity"] == "warning"


def test_rule_repair_loop_excessive_jumps():
    health = PipelineHealth(jump_count=20)
    findings = _make_watchdog()._rule_repair_loop(health)
    assert any(f["category"] == "excessive_jumps" for f in findings)
    assert any(f["severity"] == "error" for f in findings)


def test_rule_repair_loop_both():
    health = PipelineHealth(repair_attempts=5, jump_count=25)
    findings = _make_watchdog()._rule_repair_loop(health)
    assert len(findings) == 2


# ── Rule: idle_agent ───────────────────────────────────────────────


def test_rule_idle_agent_active():
    health = PipelineHealth(consecutive_readonly_turns=3)
    findings = _make_watchdog()._rule_idle_agent(health)
    assert findings == []


def test_rule_idle_agent_stuck():
    health = PipelineHealth(consecutive_readonly_turns=8)
    findings = _make_watchdog()._rule_idle_agent(health)
    assert len(findings) == 1
    assert findings[0]["category"] == "idle_agent"
    assert findings[0]["severity"] == "warning"


# ── Rule: quality_prerequisites ────────────────────────────────────


def test_rule_quality_prereqs_file_present():
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "pyproject.toml"), "w").close()
        health = PipelineHealth()
        profile = {"required_project_files": ("pyproject.toml",)}
        findings = _make_watchdog()._rule_quality_prerequisites(tmpdir, profile, health)
        assert findings == []


def test_rule_quality_prereqs_file_missing():
    with tempfile.TemporaryDirectory() as tmpdir:
        health = PipelineHealth()
        profile = {"required_project_files": ("pyproject.toml",)}
        findings = _make_watchdog()._rule_quality_prerequisites(tmpdir, profile, health)
        assert len(findings) == 1
        assert findings[0]["category"] == "quality_prereq_missing"


def test_rule_quality_prereqs_after_quality():
    with tempfile.TemporaryDirectory() as tmpdir:
        health = PipelineHealth()
        health.stages_completed.append("quality")
        profile = {"required_project_files": ("pyproject.toml",)}
        findings = _make_watchdog()._rule_quality_prerequisites(tmpdir, profile, health)
        assert findings == []


# ── Rule: cross_module_consistency ─────────────────────────────────


def test_rule_cross_module_files_exist():
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "main.py"), "w").close()
        context = {"owned_files": ["main.py"]}
        findings = _make_watchdog()._rule_cross_module_consistency(tmpdir, context)
        assert findings == []


def test_rule_cross_module_files_missing():
    with tempfile.TemporaryDirectory() as tmpdir:
        context = {"owned_files": ["missing.py", "gone.py"]}
        findings = _make_watchdog()._rule_cross_module_consistency(tmpdir, context)
        assert len(findings) == 2
        assert all(f["category"] == "owned_file_missing" for f in findings)


def test_rule_cross_module_no_owned_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        context = {}
        findings = _make_watchdog()._rule_cross_module_consistency(tmpdir, context)
        assert findings == []


# ── EventBus consumer ──────────────────────────────────────────────


@patch("trust5.core.event_bus.get_bus", return_value=None)
def test_start_event_consumer_no_bus(_mock_bus):
    health = PipelineHealth()
    thread, sub_q = _start_event_consumer(health)
    assert thread is not None
    assert thread.daemon is True
    assert sub_q is None
    thread.join(timeout=2)


@patch("trust5.core.event_bus.get_bus")
def test_start_event_consumer_with_bus(mock_get_bus):
    import queue

    mock_bus = MagicMock()
    mock_q: queue.Queue = queue.Queue()
    mock_bus.subscribe.return_value = mock_q
    mock_get_bus.return_value = mock_bus
    health = PipelineHealth()
    thread, sub_q = _start_event_consumer(health)
    assert thread is not None
    assert thread.daemon is True
    assert sub_q is mock_q
    mock_q.put(None)
    thread.join(timeout=2)


# ── LLM Auditor ───────────────────────────────────────────────────


def test_build_narrative_prompt_contains_fields():
    health = PipelineHealth(repair_attempts=2, jump_count=5)
    health.stages_completed.append("implement")
    profile = {"language": "python"}
    rule_findings = [{"severity": "warning", "category": "repair_loop"}]
    prompt = _build_narrative_prompt(health, profile, rule_findings, 120.0, 10, "")
    assert "python" in prompt.lower()
    assert "implement" in prompt
    assert "repair_loop" in prompt


def test_build_narrative_prompt_includes_previous():
    health = PipelineHealth()
    prompt = _build_narrative_prompt(health, {}, [], 60.0, 5, "Previous: all good")
    assert "Previous: all good" in prompt


def test_build_narrative_prompt_includes_test_history():
    health = PipelineHealth()
    health.test_pass_history = [True, True, False]
    prompt = _build_narrative_prompt(health, {}, [], 60.0, 5, "")
    assert "\u2713" in prompt or "passed" in prompt.lower()


@patch("trust5.core.llm.LLM")
def test_run_llm_narrative_success(mock_llm_cls):
    mock_instance = MagicMock()
    mock_instance.chat.return_value = {"content": "Pipeline is progressing well."}
    mock_llm_cls.for_tier.return_value = mock_instance
    health = PipelineHealth()
    result = _run_llm_narrative(health, {"language": "python"}, [], 60.0, 5, "")
    assert result is not None
    assert "progressing" in result
    assert health.llm_audit_count == 1


@patch("trust5.core.llm.LLM")
def test_run_llm_narrative_strips_markdown_fences(mock_llm_cls):
    mock_instance = MagicMock()
    mock_instance.chat.return_value = {"content": "```\nPipeline status: OK\n```"}
    mock_llm_cls.for_tier.return_value = mock_instance
    health = PipelineHealth()
    result = _run_llm_narrative(health, {}, [], 60.0, 1, "")
    assert result is not None
    assert "```" not in result
    assert "Pipeline status: OK" in result


@patch("trust5.core.llm.LLM")
def test_run_llm_narrative_handles_list_content(mock_llm_cls):
    mock_instance = MagicMock()
    mock_instance.chat.return_value = {"content": [{"text": "Status: running smoothly"}]}
    mock_llm_cls.for_tier.return_value = mock_instance
    health = PipelineHealth()
    result = _run_llm_narrative(health, {}, [], 60.0, 1, "")
    assert result is not None
    assert "running smoothly" in result


@patch("trust5.core.llm.LLM")
def test_run_llm_narrative_returns_none_on_failure(mock_llm_cls):
    mock_llm_cls.for_tier.side_effect = RuntimeError("API unavailable")
    health = PipelineHealth()
    result = _run_llm_narrative(health, {}, [], 60.0, 1, "")
    assert result is None


@patch("trust5.core.llm.LLM")
def test_run_llm_narrative_returns_none_on_empty(mock_llm_cls):
    mock_instance = MagicMock()
    mock_instance.chat.return_value = {"content": ""}
    mock_llm_cls.for_tier.return_value = mock_instance
    health = PipelineHealth()
    result = _run_llm_narrative(health, {}, [], 60.0, 1, "")
    assert result is None


# ── Enhanced report format ─────────────────────────────────────────


def test_write_report_with_health_and_narrative():
    with tempfile.TemporaryDirectory() as tmpdir:
        health = PipelineHealth(repair_attempts=2, jump_count=5)
        health.stages_completed.append("implement")
        WatchdogTask._write_report(tmpdir, [], 10, health=health, narrative="Pipeline is on track.")
        report_path = os.path.join(tmpdir, ".trust5", "watchdog_report.json")
        with open(report_path) as f:
            data = json.load(f)
        assert "pipeline_health" in data
        assert data["pipeline_health"]["repair_attempts"] == 2
        assert "narrative" in data
        assert data["narrative"] == "Pipeline is on track."


def test_write_report_without_health():
    with tempfile.TemporaryDirectory() as tmpdir:
        WatchdogTask._write_report(tmpdir, [], 1)
        report_path = os.path.join(tmpdir, ".trust5", "watchdog_report.json")
        with open(report_path) as f:
            data = json.load(f)
        assert "pipeline_health" not in data
        assert "narrative" not in data


def test_load_watchdog_findings_with_narrative():
    with tempfile.TemporaryDirectory() as tmpdir:
        findings = [{"severity": "warning", "category": "test", "file": "x.py", "message": "test msg"}]
        health = PipelineHealth(repair_attempts=1)
        WatchdogTask._write_report(tmpdir, findings, 5, health=health, narrative="Repair in progress.")
        result = load_watchdog_findings(tmpdir)
        assert "Current Pipeline Assessment" in result
        assert "Repair in progress." in result
        assert "Detected Issues" in result
        assert "test msg" in result


def test_load_watchdog_findings_narrative_only():
    """Narrative without findings still produces output."""
    with tempfile.TemporaryDirectory() as tmpdir:
        WatchdogTask._write_report(tmpdir, [], 5, narrative="All clear, pipeline running.")
        result = load_watchdog_findings(tmpdir)
        assert "Current Pipeline Assessment" in result
        assert "All clear, pipeline running." in result
        assert "Detected Issues" not in result


# ── _run_rules integration ─────────────────────────────────────────


@patch("trust5.tasks.watchdog_task.emit")
def test_run_rules_aggregates_all_rules(_mock_emit):
    with tempfile.TemporaryDirectory() as tmpdir:
        health = PipelineHealth(repair_attempts=4, jump_count=25, consecutive_readonly_turns=10)
        health.stages_completed.append("implement")
        profile = {
            "tool_check_commands": ("nonexistent_xyz999 --version",),
            "test_discovery_command": "pytest --collect-only",
            "extensions": (".py",),
            "manifest_validators": (),
            "required_project_files": ("pyproject.toml",),
        }
        context: dict = {}
        findings: list[dict[str, str]] = []
        _make_watchdog()._run_rules(tmpdir, profile, context, health, findings)
        categories = {f["category"] for f in findings}
        assert "tool_missing" in categories
        assert "no_tests" in categories
        assert "repair_loop" in categories
        assert "excessive_jumps" in categories
        assert "idle_agent" in categories
        assert "quality_prereq_missing" in categories


# ── Constants ────────────────────────────────────────────────────


# ── Rebuild sentinel tests ───────────────────────────────────────


@patch("trust5.tasks.watchdog_task.emit")
def test_signal_rebuild_creates_sentinel(_mock_emit):
    """signal_rebuild writes a JSON sentinel file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        signal_rebuild(tmpdir, "test regression detected")
        signaled, reason = check_rebuild_signal(tmpdir)
        assert signaled is True
        assert "test regression" in reason


def test_check_rebuild_signal_returns_false_when_absent():
    """No sentinel file → (False, "")."""
    with tempfile.TemporaryDirectory() as tmpdir:
        signaled, reason = check_rebuild_signal(tmpdir)
        assert signaled is False
        assert reason == ""


@patch("trust5.tasks.watchdog_task.emit")
def test_clear_rebuild_signal_removes_sentinel(_mock_emit):
    """clear_rebuild_signal removes the sentinel."""
    with tempfile.TemporaryDirectory() as tmpdir:
        signal_rebuild(tmpdir, "some reason")
        clear_rebuild_signal(tmpdir)
        signaled, _ = check_rebuild_signal(tmpdir)
        assert signaled is False


def test_clear_rebuild_signal_noop_when_absent():
    """clear_rebuild_signal doesn't fail if no sentinel exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        clear_rebuild_signal(tmpdir)  # Should not raise


# ── Auto-delete garbled files tests ──────────────────────────────


@patch("trust5.tasks.watchdog_task.emit")
def test_check_garbled_files_auto_deletes(_mock_emit):
    """Garbled files should be auto-deleted, not just reported."""
    with tempfile.TemporaryDirectory() as tmpdir:
        garbled_path = os.path.join(tmpdir, "=3.0.0")
        open(garbled_path, "w").close()
        findings: list[dict[str, str]] = []
        _make_watchdog()._check_garbled_files(tmpdir, findings)
        assert not os.path.exists(garbled_path), "Garbled file should be deleted"
        assert len(findings) >= 1
        assert "auto-deleted" in findings[0]["message"].lower() or "garbled" in findings[0]["message"].lower()


# ── PipelineHealth enhancement tests ─────────────────────────────


def test_pipeline_health_record_test_result():
    """PipelineHealth.record_test_result tracks pass/fail history."""
    h = PipelineHealth()
    h.record_test_result(True)
    h.record_test_result(False)
    h.record_test_result(True)
    assert h.test_pass_history == [True, False, True]


def test_pipeline_health_to_dict_includes_new_fields():
    """to_dict() should include test_pass_history and last_stage_completion_time."""
    h = PipelineHealth()
    h.record_test_result(True)
    h.last_stage_completion_time = 12345.0
    d = h.to_dict()
    assert "test_pass_history" in d
    assert d["test_pass_history"] == [True]
    assert "last_stage_completion_time" in d
    assert d["last_stage_completion_time"] == 12345.0


# ── New rule tests ───────────────────────────────────────────────


@patch("trust5.tasks.watchdog_task.emit")
def test_rule_regression_detects_decline(_mock_emit):
    """Rule 8: 3 consecutive failures after a pass triggers regression warning."""
    health = PipelineHealth()
    health.test_pass_history = [True, True, False, False, False]
    findings = WatchdogTask._rule_regression(health)
    assert len(findings) == 1
    assert findings[0]["category"] == "regression"
    assert findings[0]["severity"] == "error"


@patch("trust5.tasks.watchdog_task.emit")
def test_rule_regression_no_alert_on_short_history(_mock_emit):
    """Rule 8: Less than 4 results → no finding."""
    health = PipelineHealth()
    health.test_pass_history = [False, False, False]
    findings = WatchdogTask._rule_regression(health)
    assert len(findings) == 0


@patch("trust5.tasks.watchdog_task.emit")
def test_rule_regression_no_alert_when_never_passed(_mock_emit):
    """Rule 8: All failures (never passed) → no regression (it was never good)."""
    health = PipelineHealth()
    health.test_pass_history = [False, False, False, False, False]
    findings = WatchdogTask._rule_regression(health)
    assert len(findings) == 0


@patch("trust5.tasks.watchdog_task.emit")
def test_rule_stall_detects_long_gap(_mock_emit):
    """Rule 9: No stage completion for >30 min → stall warning."""
    import time as _time

    health = PipelineHealth()
    health.last_stage_completion_time = _time.monotonic() - 2000  # 33+ minutes ago
    findings = WatchdogTask._rule_stall(health)
    assert len(findings) == 1
    assert findings[0]["category"] == "pipeline_stall"


@patch("trust5.tasks.watchdog_task.emit")
def test_rule_stall_no_alert_when_recent(_mock_emit):
    """Rule 9: Recent stage completion → no stall."""
    import time as _time

    health = PipelineHealth()
    health.last_stage_completion_time = _time.monotonic() - 60  # 1 minute ago
    findings = WatchdogTask._rule_stall(health)
    assert len(findings) == 0


@patch("trust5.tasks.watchdog_task.emit")
def test_rule_exhaustion_warns_at_60_percent(_mock_emit):
    """Rule 10: Jump count at 60%+ of limit → warning."""
    health = PipelineHealth()
    health.jump_count = 31
    context = {"_max_jumps": 50}
    findings = WatchdogTask._rule_exhaustion(health, context)
    assert len(findings) == 1
    assert findings[0]["severity"] == "warning"
    assert findings[0]["category"] == "jump_exhaustion"


@patch("trust5.tasks.watchdog_task.emit")
def test_rule_exhaustion_errors_at_80_percent(_mock_emit):
    """Rule 10: Jump count at 80%+ of limit → error."""
    health = PipelineHealth()
    health.jump_count = 42
    context = {"_max_jumps": 50}
    findings = WatchdogTask._rule_exhaustion(health, context)
    assert len(findings) == 1
    assert findings[0]["severity"] == "error"


@patch("trust5.tasks.watchdog_task.emit")
def test_rule_exhaustion_no_alert_below_threshold(_mock_emit):
    """Rule 10: Jump count below 60% → no finding."""
    health = PipelineHealth()
    health.jump_count = 10
    context = {"_max_jumps": 50}
    findings = WatchdogTask._rule_exhaustion(health, context)
    assert len(findings) == 0


# ── Rebuild trigger tests ────────────────────────────────────────


@patch("trust5.tasks.watchdog_task.emit")
def test_should_trigger_rebuild_on_regression_and_high_jumps(_mock_emit):
    """Rebuild triggers when jump count >= 80% AND recent regression."""
    with tempfile.TemporaryDirectory() as tmpdir:
        health = PipelineHealth()
        health.jump_count = 42
        health.test_pass_history = [True, True, False, False, False]
        context: dict = {"_max_jumps": 50}
        result = _make_watchdog()._should_trigger_rebuild(health, context, tmpdir)
        assert result is True
        signaled, _ = check_rebuild_signal(tmpdir)
        assert signaled is True
        clear_rebuild_signal(tmpdir)


@patch("trust5.tasks.watchdog_task.emit")
def test_should_trigger_rebuild_false_when_jumps_low(_mock_emit):
    """No rebuild when jump count is below 80%."""
    with tempfile.TemporaryDirectory() as tmpdir:
        health = PipelineHealth()
        health.jump_count = 10
        health.test_pass_history = [True, False, False, False]
        context: dict = {"_max_jumps": 50}
        result = _make_watchdog()._should_trigger_rebuild(health, context, tmpdir)
        assert result is False
