"""Tests for WatchdogTask in trust5/tasks/watchdog_task.py.

Tests cover:
  - _collect_filesystem_summary (LLM context building)
  - _write_report / load_watchdog_findings (report persistence + LLM injection)
  - _emit_narrative_block (TUI feedback)
  - Sentinel / pipeline completion helpers
  - PipelineHealth state machine
  - EventBus consumer
  - LLM Narrator (_build_narrative_prompt, _run_llm_narrative)
  - Behavioral rules (repair_loop, idle_agent, regression, stall, exhaustion)
  - _run_behavioral_rules integration
  - Rebuild sentinel + trigger logic
"""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

from trust5.tasks.watchdog_task import (
    PipelineHealth,
    WatchdogTask,
    _build_narrative_prompt,
    _collect_filesystem_summary,
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


# ── _collect_filesystem_summary ──────────────────────────────────────


def test_collect_filesystem_summary_lists_files():
    """Filesystem summary includes file names and sizes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "main.py"), "w") as f:
            f.write("print('hello')\n")
        open(os.path.join(tmpdir, "README.md"), "w").close()
        result = _collect_filesystem_summary(tmpdir)
        assert "main.py" in result
        assert "README.md" in result
        assert "bytes" in result


def test_collect_filesystem_summary_empty_dir():
    """Empty project directory returns placeholder text."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _collect_filesystem_summary(tmpdir)
        assert "empty project directory" in result


def test_collect_filesystem_summary_skips_dirs():
    """Skipped directories (node_modules, .git, etc.) are excluded."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nm = os.path.join(tmpdir, "node_modules")
        os.makedirs(nm)
        with open(os.path.join(nm, "dep.js"), "w") as f:
            f.write("// dep")
        git_dir = os.path.join(tmpdir, ".git")
        os.makedirs(git_dir)
        with open(os.path.join(git_dir, "HEAD"), "w") as f:
            f.write("ref: refs/heads/main")
        with open(os.path.join(tmpdir, "app.py"), "w") as f:
            f.write("print('app')\n")
        result = _collect_filesystem_summary(tmpdir)
        assert "app.py" in result
        assert "dep.js" not in result
        assert "HEAD" not in result


def test_collect_filesystem_summary_truncates():
    """Summary truncates after max_files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        for i in range(20):
            open(os.path.join(tmpdir, f"file_{i}.txt"), "w").close()
        result = _collect_filesystem_summary(tmpdir, max_files=5)
        assert "truncated" in result


def test_collect_filesystem_summary_shows_sizes():
    """File sizes are shown in bytes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "big.py"), "w") as f:
            f.write("x" * 1000)
        result = _collect_filesystem_summary(tmpdir)
        assert "1000 bytes" in result


def test_collect_filesystem_summary_skips_trust5_dir():
    """.trust5 directory is excluded from the listing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        trust5_dir = os.path.join(tmpdir, ".trust5")
        os.makedirs(trust5_dir)
        with open(os.path.join(trust5_dir, "trust5.db"), "w") as f:
            f.write("data")
        with open(os.path.join(tmpdir, "src.py"), "w") as f:
            f.write("code")
        result = _collect_filesystem_summary(tmpdir)
        assert "src.py" in result
        assert "trust5.db" not in result


def test_collect_filesystem_summary_nested_dirs():
    """Files in nested subdirectories are listed with relative paths."""
    with tempfile.TemporaryDirectory() as tmpdir:
        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        with open(os.path.join(src, "main.go"), "w") as f:
            f.write("package main")
        result = _collect_filesystem_summary(tmpdir)
        assert os.path.join("src", "main.go") in result


# ── _write_report ───────────────────────────────────────────────────


def test_write_report_creates_valid_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        findings = [
            {"severity": "error", "category": "repair_loop", "message": "Stuck in repair loop"},
            {"severity": "warning", "category": "idle_agent", "message": "Agent idle"},
        ]
        WatchdogTask._write_report(tmpdir, findings, 7)
        report_path = os.path.join(tmpdir, ".trust5", "watchdog_report.json")
        assert os.path.exists(report_path)
        with open(report_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["check_number"] == 7
        assert len(data["findings"]) == 2
        assert data["findings"][0]["severity"] == "error"
        assert data["findings"][1]["category"] == "idle_agent"


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


# ── load_watchdog_findings ──────────────────────────────────────────


def test_load_watchdog_findings_no_report():
    """Returns empty string when no report file exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = load_watchdog_findings(tmpdir)
        assert result == ""


def test_load_watchdog_findings_empty_findings():
    """Returns empty string when report has no findings and no narrative."""
    with tempfile.TemporaryDirectory() as tmpdir:
        WatchdogTask._write_report(tmpdir, [], 5)
        result = load_watchdog_findings(tmpdir)
        assert result == ""


def test_load_watchdog_findings_with_findings():
    """Returns formatted markdown when findings exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        findings = [
            {"severity": "error", "category": "repair_loop", "message": "Pipeline stuck in repair loop"},
            {"severity": "warning", "category": "idle_agent", "message": "Agent appears idle"},
        ]
        WatchdogTask._write_report(tmpdir, findings, 2)
        result = load_watchdog_findings(tmpdir)
        assert "## Watchdog Pipeline Status" in result
        assert "**[ERROR]**" in result
        assert "**[WARNING]**" in result
        assert "(repair_loop)" in result
        assert "(idle_agent)" in result


def test_load_watchdog_findings_corrupt_json():
    """Returns empty string when report JSON is corrupted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        trust5_dir = os.path.join(tmpdir, ".trust5")
        os.makedirs(trust5_dir)
        with open(os.path.join(trust5_dir, "watchdog_report.json"), "w") as f:
            f.write("{not valid json")
        result = load_watchdog_findings(tmpdir)
        assert result == ""


def test_load_watchdog_findings_with_narrative():
    with tempfile.TemporaryDirectory() as tmpdir:
        findings = [{"severity": "warning", "category": "idle_agent", "message": "Agent stuck reading"}]
        health = PipelineHealth(repair_attempts=1)
        WatchdogTask._write_report(tmpdir, findings, 5, health=health, narrative="Repair in progress.")
        result = load_watchdog_findings(tmpdir)
        assert "Current Pipeline Assessment" in result
        assert "Repair in progress." in result
        assert "Detected Issues" in result
        assert "Agent stuck reading" in result


def test_load_watchdog_findings_narrative_only():
    """Narrative without findings still produces output."""
    with tempfile.TemporaryDirectory() as tmpdir:
        WatchdogTask._write_report(tmpdir, [], 5, narrative="All clear, pipeline running.")
        result = load_watchdog_findings(tmpdir)
        assert "Current Pipeline Assessment" in result
        assert "All clear, pipeline running." in result
        assert "Detected Issues" not in result


# ── _emit_narrative_block ──────────────────────────────────────────


@patch("trust5.tasks.watchdog_task.emit_block")
def test_emit_narrative_block_with_warnings(mock_emit_block):
    """Narrative block with warning-level findings uses WDWN code."""
    findings = [
        {"severity": "warning", "category": "idle_agent", "message": "Agent idle"},
    ]
    WatchdogTask._emit_narrative_block("Pipeline is recovering.", findings, 3)
    mock_emit_block.assert_called_once()
    args = mock_emit_block.call_args
    from trust5.core.message import M as _M

    assert args[0][0] == _M.WDWN
    assert "Watchdog (check #3)" in args[0][1]
    assert "Pipeline is recovering." in args[0][2]
    assert "idle_agent" in args[0][2]
    assert "\u26a0\ufe0f" in args[0][2]


@patch("trust5.tasks.watchdog_task.emit_block")
def test_emit_narrative_block_error_escalates(mock_emit_block):
    """Narrative block with error-level findings uses WDER code."""
    findings = [
        {"severity": "warning", "category": "idle_agent", "message": "Agent idle"},
        {"severity": "error", "category": "repair_loop", "message": "Stuck in loop"},
    ]
    WatchdogTask._emit_narrative_block("Pipeline struggling.", findings, 7)
    mock_emit_block.assert_called_once()
    args = mock_emit_block.call_args
    from trust5.core.message import M as _M

    assert args[0][0] == _M.WDER
    assert "\u274c" in args[0][2]
    assert "\u26a0\ufe0f" in args[0][2]


@patch("trust5.tasks.watchdog_task.emit_block")
def test_emit_narrative_block_content_format(mock_emit_block):
    """Block content includes narrative, severity, category, and message."""
    findings = [
        {"severity": "error", "category": "regression", "message": "Test regression detected"},
    ]
    WatchdogTask._emit_narrative_block("Check results:", findings, 1)
    content = mock_emit_block.call_args[0][2]
    assert "Check results:" in content
    assert "[ERROR]" in content
    assert "regression" in content
    assert "Test regression detected" in content


@patch("trust5.tasks.watchdog_task.emit_block")
def test_emit_narrative_block_narrative_only(mock_emit_block):
    """Narrative-only block (no behavioral findings)."""
    WatchdogTask._emit_narrative_block("All systems nominal.", [], 4)
    content = mock_emit_block.call_args[0][2]
    assert "All systems nominal." in content


@patch("trust5.tasks.watchdog_task.emit_block")
def test_emit_narrative_block_empty_skips(mock_emit_block):
    """Empty narrative and no findings → no emit."""
    WatchdogTask._emit_narrative_block("", [], 1)
    mock_emit_block.assert_not_called()


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
        WatchdogTask._clear_sentinel(tmpdir)


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


def test_pipeline_health_record_test_result():
    """PipelineHealth.record_test_result tracks pass/fail history."""
    h = PipelineHealth()
    h.record_test_result(True)
    h.record_test_result(False)
    h.record_test_result(True)
    assert h.test_pass_history == [True, False, True]


def test_pipeline_health_to_dict_includes_all_fields():
    """to_dict() includes test_pass_history and last_stage_completion_time."""
    h = PipelineHealth()
    h.record_test_result(True)
    h.last_stage_completion_time = 12345.0
    d = h.to_dict()
    assert "test_pass_history" in d
    assert d["test_pass_history"] == [True]
    assert "last_stage_completion_time" in d
    assert d["last_stage_completion_time"] == 12345.0


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


# ── LLM Narrator ──────────────────────────────────────────────────


def test_build_narrative_prompt_contains_fields():
    health = PipelineHealth(repair_attempts=2, jump_count=5)
    health.stages_completed.append("implement")
    profile = {"language": "python"}
    rule_findings = [{"severity": "warning", "category": "repair_loop", "message": "loop"}]
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


def test_build_narrative_prompt_includes_filesystem_summary():
    """Filesystem summary appears in the prompt for LLM assessment."""
    health = PipelineHealth()
    fs_summary = "  main.py (150 bytes)\n  README.md (0 bytes)"
    prompt = _build_narrative_prompt(health, {}, [], 60.0, 5, "", filesystem_summary=fs_summary)
    assert "main.py (150 bytes)" in prompt
    assert "README.md (0 bytes)" in prompt
    assert "PROJECT FILES" in prompt


def test_build_narrative_prompt_no_filesystem_summary():
    """When filesystem summary is empty, prompt shows placeholder."""
    health = PipelineHealth()
    prompt = _build_narrative_prompt(health, {}, [], 60.0, 5, "")
    assert "(not available)" in prompt


def test_build_narrative_prompt_empty_behavioral_findings():
    """When no behavioral findings, prompt shows 'None'."""
    health = PipelineHealth()
    prompt = _build_narrative_prompt(health, {}, [], 60.0, 1, "")
    assert "BEHAVIORAL ALERTS" in prompt
    assert "None" in prompt


@patch("trust5.core.llm.LLM")
def test_run_llm_narrative_success(mock_llm_cls):
    mock_instance = MagicMock()
    mock_instance.chat.return_value = {
        "message": {"role": "assistant", "content": "Pipeline is progressing well."},
        "done": True,
    }
    mock_llm_cls.for_tier.return_value = mock_instance
    health = PipelineHealth()
    result = _run_llm_narrative(health, {"language": "python"}, [], 60.0, 5, "")
    assert result is not None
    assert "progressing" in result
    assert health.llm_audit_count == 1


@patch("trust5.core.llm.LLM")
def test_run_llm_narrative_with_filesystem_summary(mock_llm_cls):
    """Filesystem summary is passed through to the LLM prompt."""
    mock_instance = MagicMock()
    mock_instance.chat.return_value = {
        "message": {"role": "assistant", "content": "Project structure looks correct."},
        "done": True,
    }
    mock_llm_cls.for_tier.return_value = mock_instance
    health = PipelineHealth()
    result = _run_llm_narrative(
        health,
        {},
        [],
        60.0,
        1,
        "",
        filesystem_summary="  main.py (100 bytes)",
    )
    assert result is not None
    assert "Project structure" in result
    # Verify the prompt sent to LLM contains the filesystem summary
    call_args = mock_instance.chat.call_args
    prompt = call_args[1]["messages"][0]["content"]
    assert "main.py (100 bytes)" in prompt


@patch("trust5.core.llm.LLM")
def test_run_llm_narrative_strips_markdown_fences(mock_llm_cls):
    mock_instance = MagicMock()
    mock_instance.chat.return_value = {
        "message": {"role": "assistant", "content": "```\nPipeline status: OK\n```"},
        "done": True,
    }
    mock_llm_cls.for_tier.return_value = mock_instance
    health = PipelineHealth()
    result = _run_llm_narrative(health, {}, [], 60.0, 1, "")
    assert result is not None
    assert "```" not in result
    assert "Pipeline status: OK" in result


@patch("trust5.core.llm.LLM")
def test_run_llm_narrative_handles_list_content(mock_llm_cls):
    mock_instance = MagicMock()
    mock_instance.chat.return_value = {
        "message": {
            "role": "assistant",
            "content": [{"text": "Status: running smoothly"}],
        },
        "done": True,
    }
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
    mock_instance.chat.return_value = {"message": {"role": "assistant", "content": ""}, "done": True}
    mock_llm_cls.for_tier.return_value = mock_instance
    health = PipelineHealth()
    result = _run_llm_narrative(health, {}, [], 60.0, 1, "")
    assert result is None


# ── Behavioral rules ──────────────────────────────────────────────


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


def test_rule_regression_detects_decline():
    """3 consecutive failures after a pass triggers regression error."""
    health = PipelineHealth()
    health.test_pass_history = [True, True, False, False, False]
    findings = WatchdogTask._rule_regression(health)
    assert len(findings) == 1
    assert findings[0]["category"] == "regression"
    assert findings[0]["severity"] == "error"


def test_rule_regression_no_alert_on_short_history():
    """Less than 4 results → no finding."""
    health = PipelineHealth()
    health.test_pass_history = [False, False, False]
    findings = WatchdogTask._rule_regression(health)
    assert len(findings) == 0


def test_rule_regression_no_alert_when_never_passed():
    """All failures (never passed) → no regression (it was never good)."""
    health = PipelineHealth()
    health.test_pass_history = [False, False, False, False, False]
    findings = WatchdogTask._rule_regression(health)
    assert len(findings) == 0


def test_rule_stall_detects_long_gap():
    """No stage completion for >30 min → stall warning."""
    import time as _time

    health = PipelineHealth()
    health.last_stage_completion_time = _time.monotonic() - 2000  # 33+ minutes ago
    findings = WatchdogTask._rule_stall(health)
    assert len(findings) == 1
    assert findings[0]["category"] == "pipeline_stall"


def test_rule_stall_no_alert_when_recent():
    """Recent stage completion → no stall."""
    import time as _time

    health = PipelineHealth()
    health.last_stage_completion_time = _time.monotonic() - 60  # 1 minute ago
    findings = WatchdogTask._rule_stall(health)
    assert len(findings) == 0


def test_rule_stall_no_alert_when_never_started():
    """No stage completed yet (time=0) → no stall."""
    health = PipelineHealth()
    health.last_stage_completion_time = 0
    findings = WatchdogTask._rule_stall(health)
    assert len(findings) == 0


def test_rule_exhaustion_warns_at_60_percent():
    """Jump count at 60%+ of limit → warning."""
    health = PipelineHealth()
    health.jump_count = 31
    context = {"_max_jumps": 50}
    findings = WatchdogTask._rule_exhaustion(health, context)
    assert len(findings) == 1
    assert findings[0]["severity"] == "warning"
    assert findings[0]["category"] == "jump_exhaustion"


def test_rule_exhaustion_errors_at_80_percent():
    """Jump count at 80%+ of limit → error."""
    health = PipelineHealth()
    health.jump_count = 42
    context = {"_max_jumps": 50}
    findings = WatchdogTask._rule_exhaustion(health, context)
    assert len(findings) == 1
    assert findings[0]["severity"] == "error"


def test_rule_exhaustion_no_alert_below_threshold():
    """Jump count below 60% → no finding."""
    health = PipelineHealth()
    health.jump_count = 10
    context = {"_max_jumps": 50}
    findings = WatchdogTask._rule_exhaustion(health, context)
    assert len(findings) == 0


def test_rule_exhaustion_zero_max_jumps():
    """Zero max_jumps → no finding (avoids division by zero)."""
    health = PipelineHealth()
    health.jump_count = 10
    context = {"_max_jumps": 0}
    findings = WatchdogTask._rule_exhaustion(health, context)
    assert len(findings) == 0


# ── _run_behavioral_rules integration ─────────────────────────────


def test_run_behavioral_rules_aggregates():
    """_run_behavioral_rules aggregates findings from all behavioral rules."""
    health = PipelineHealth(repair_attempts=4, jump_count=35, consecutive_readonly_turns=10)
    context: dict = {"_max_jumps": 50}
    findings: list[dict[str, str]] = []
    _make_watchdog()._run_behavioral_rules(health, context, findings)
    categories = {f["category"] for f in findings}
    assert "repair_loop" in categories
    assert "excessive_jumps" in categories
    assert "idle_agent" in categories
    assert "jump_exhaustion" in categories


def test_run_behavioral_rules_clean():
    """_run_behavioral_rules returns no findings for healthy pipeline."""
    health = PipelineHealth(repair_attempts=0, jump_count=2, consecutive_readonly_turns=0)
    context: dict = {"_max_jumps": 50}
    findings: list[dict[str, str]] = []
    _make_watchdog()._run_behavioral_rules(health, context, findings)
    assert findings == []


def test_run_behavioral_rules_with_regression():
    """_run_behavioral_rules includes regression when test history shows decline."""
    health = PipelineHealth()
    health.test_pass_history = [True, True, False, False, False]
    context: dict = {"_max_jumps": 50}
    findings: list[dict[str, str]] = []
    _make_watchdog()._run_behavioral_rules(health, context, findings)
    categories = {f["category"] for f in findings}
    assert "regression" in categories


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


def test_check_rebuild_signal_corrupt_json():
    """Corrupt sentinel JSON still returns (True, fallback reason)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sentinel_dir = os.path.join(tmpdir, ".trust5")
        os.makedirs(sentinel_dir)
        with open(os.path.join(sentinel_dir, "watchdog_rebuild"), "w") as f:
            f.write("{bad json")
        signaled, reason = check_rebuild_signal(tmpdir)
        assert signaled is True
        assert "unreadable" in reason


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


@patch("trust5.tasks.watchdog_task.emit")
def test_should_trigger_rebuild_on_stall_and_high_jumps(_mock_emit):
    """Rebuild triggers when jump count >= 80% AND pipeline stalled."""
    import time as _time

    with tempfile.TemporaryDirectory() as tmpdir:
        health = PipelineHealth()
        health.jump_count = 42
        health.last_stage_completion_time = _time.monotonic() - 2000  # 33+ min stall
        context: dict = {"_max_jumps": 50}
        result = _make_watchdog()._should_trigger_rebuild(health, context, tmpdir)
        assert result is True
        clear_rebuild_signal(tmpdir)


@patch("trust5.tasks.watchdog_task.emit")
def test_should_trigger_rebuild_false_when_progressing(_mock_emit):
    """No rebuild when jump count is high but tests are passing."""
    import time as _time

    with tempfile.TemporaryDirectory() as tmpdir:
        health = PipelineHealth()
        health.jump_count = 42
        health.test_pass_history = [True, True, True]  # No regression
        health.last_stage_completion_time = _time.monotonic() - 60  # Not stalled
        context: dict = {"_max_jumps": 50}
        result = _make_watchdog()._should_trigger_rebuild(health, context, tmpdir)
        assert result is False
