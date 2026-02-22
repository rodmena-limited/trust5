"""Tests for trust5/tasks/repair_task.py — RepairTask class."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from stabilize.errors import TransientError
from stabilize.models.status import WorkflowStatus

from trust5.core.llm import LLMError
from trust5.tasks.repair_task import RepairTask


def make_stage(context: dict | None = None) -> MagicMock:
    stage = MagicMock()
    stage.context = context or {}
    stage.context.setdefault("project_root", "/tmp/fake-project")
    return stage


@patch("trust5.tasks.repair_task.emit")
def test_repair_skips_when_not_requested(mock_emit):
    """When _repair_requested is False (DAG start, not jump), skip repair."""
    task = RepairTask()
    stage = make_stage(
        {
            "_repair_requested": False,
            "test_output": "some failures",
            "tests_passed": False,
        }
    )

    result = task.execute(stage)

    assert result.status == WorkflowStatus.SUCCEEDED
    assert result.outputs["repair_skipped"] is True


@patch("trust5.tasks.repair_task.emit")
def test_repair_skips_when_tests_passed(mock_emit):
    """When tests_passed=True and failure_type is not quality, skip repair
    but jump back to validate so its stage.completed event fires."""
    task = RepairTask()
    stage = make_stage(
        {
            "_repair_requested": True,
            "test_output": "all green",
            "tests_passed": True,
            "tests_partial": False,
            "failure_type": "test",
        }
    )

    result = task.execute(stage)

    assert result.status == WorkflowStatus.REDIRECT
    assert result.target_stage_ref_id == "validate"
    assert result.outputs["repair_skipped"] is True


@patch("trust5.tasks.repair_task.emit")
def test_repair_skips_when_tests_partial(mock_emit):
    """When tests_partial=True and failure_type is not quality, skip repair
    but jump back to validate so its stage.completed event fires."""
    task = RepairTask()
    stage = make_stage(
        {
            "_repair_requested": True,
            "test_output": "partial output",
            "tests_passed": False,
            "tests_partial": True,
            "failure_type": "test",
        }
    )

    result = task.execute(stage)

    assert result.status == WorkflowStatus.REDIRECT
    assert result.target_stage_ref_id == "validate"
    assert result.outputs["repair_skipped"] is True


@patch("trust5.tasks.repair_task.emit")
@patch("trust5.tasks.repair_task.Agent")
@patch("trust5.tasks.repair_task.LLM")
@patch("trust5.tasks.repair_task.summarize_errors", return_value="summarized errors")
@patch("trust5.tasks.repair_task.build_repair_prompt", return_value="fix this code")
@patch("trust5.tasks.repair_task.propagate_context")
def test_repair_jumps_to_validate_after_fix(
    mock_propagate, mock_prompt, mock_summarize, mock_llm_cls, mock_agent_cls, mock_emit
):
    """Normal repair: agent runs, then jump_to('validate')."""
    mock_agent = MagicMock()
    mock_agent.run.return_value = "Fixed the bug in main.py"
    mock_agent_cls.return_value = mock_agent
    mock_llm_cls.for_tier.return_value = MagicMock()

    task = RepairTask()
    stage = make_stage(
        {
            "_repair_requested": True,
            "test_output": "FAILED test_foo - AssertionError",
            "tests_passed": False,
            "tests_partial": False,
            "failure_type": "test",
            "repair_attempt": 1,
        }
    )

    result = task.execute(stage)

    assert result.status == WorkflowStatus.REDIRECT
    assert result.target_stage_ref_id == "validate"
    assert result.outputs["repair_result"] is not None
    mock_agent.run.assert_called_once()
    mock_propagate.assert_called()


@patch("trust5.tasks.repair_task.emit")
@patch("trust5.tasks.repair_task.Agent")
@patch("trust5.tasks.repair_task.LLM")
@patch("trust5.tasks.repair_task.summarize_errors", return_value="quality issues")
@patch("trust5.tasks.repair_task.build_repair_prompt", return_value="fix quality")
@patch("trust5.tasks.repair_task.propagate_context")
def test_repair_jumps_to_quality_for_quality_failure(
    mock_propagate, mock_prompt, mock_summarize, mock_llm_cls, mock_agent_cls, mock_emit
):
    """When failure_type='quality', jump to 'quality' instead of 'validate'."""
    mock_agent = MagicMock()
    mock_agent.run.return_value = "Fixed lint issues"
    mock_agent_cls.return_value = mock_agent
    mock_llm_cls.for_tier.return_value = MagicMock()

    task = RepairTask()
    stage = make_stage(
        {
            "_repair_requested": True,
            "test_output": "TRUST 5 QUALITY GATE FAILED",
            "tests_passed": True,  # quality failures can have passing tests
            "tests_partial": False,
            "failure_type": "quality",
            "repair_attempt": 1,
            "quality_attempt": 1,
            "max_quality_attempts": 3,
        }
    )

    result = task.execute(stage)

    assert result.status == WorkflowStatus.REDIRECT
    assert result.target_stage_ref_id == "quality"
    assert result.context.get("quality_attempt") == 1


@patch("trust5.tasks.repair_task.emit")
@patch("trust5.tasks.repair_task.Agent")
@patch("trust5.tasks.repair_task.LLM")
@patch("trust5.tasks.repair_task.summarize_errors", return_value="errors")
@patch("trust5.tasks.repair_task.build_repair_prompt", return_value="fix")
def test_repair_no_chdir(mock_prompt, mock_summarize, mock_llm_cls, mock_agent_cls, mock_emit):
    """Verify os.chdir is NOT called during repair execution."""
    mock_agent = MagicMock()
    mock_agent.run.return_value = "done"
    mock_agent_cls.return_value = mock_agent
    mock_llm_cls.for_tier.return_value = MagicMock()

    task = RepairTask()
    stage = make_stage(
        {
            "_repair_requested": True,
            "test_output": "FAILED",
            "tests_passed": False,
            "tests_partial": False,
            "failure_type": "test",
            "repair_attempt": 1,
        }
    )

    with patch("os.chdir") as mock_chdir:
        task.execute(stage)
        mock_chdir.assert_not_called()


@patch("trust5.tasks.repair_task.emit")
@patch("trust5.tasks.repair_task.Agent")
@patch("trust5.tasks.repair_task.LLM")
@patch("trust5.tasks.repair_task.summarize_errors", return_value="errors")
@patch("trust5.tasks.repair_task.build_repair_prompt", return_value="fix")
def test_repair_llm_transient_error_raises(mock_prompt, mock_summarize, mock_llm_cls, mock_agent_cls, mock_emit):
    """LLMError with retryable=True raises TransientError for Stabilize retry."""
    mock_agent = MagicMock()
    mock_agent.run.side_effect = LLMError("rate limited", retryable=True, retry_after=60, error_class="rate_limit")
    mock_agent_cls.return_value = mock_agent
    mock_llm_cls.for_tier.return_value = MagicMock()

    task = RepairTask()
    stage = make_stage(
        {
            "_repair_requested": True,
            "test_output": "FAILED",
            "tests_passed": False,
            "tests_partial": False,
            "failure_type": "test",
            "repair_attempt": 1,
        }
    )

    with pytest.raises(TransientError):
        task.execute(stage)


@patch("trust5.tasks.repair_task.emit")
@patch("trust5.tasks.repair_task.Agent")
@patch("trust5.tasks.repair_task.LLM")
@patch("trust5.tasks.repair_task.summarize_errors", return_value="errors")
@patch("trust5.tasks.repair_task.build_repair_prompt", return_value="fix")
def test_repair_llm_permanent_error_terminal(mock_prompt, mock_summarize, mock_llm_cls, mock_agent_cls, mock_emit):
    """LLMError with retryable=False returns TaskResult.terminal()."""
    mock_agent = MagicMock()
    mock_agent.run.side_effect = LLMError("invalid API key", retryable=False, error_class="permanent")
    mock_agent_cls.return_value = mock_agent
    mock_llm_cls.for_tier.return_value = MagicMock()

    task = RepairTask()
    stage = make_stage(
        {
            "_repair_requested": True,
            "test_output": "FAILED",
            "tests_passed": False,
            "tests_partial": False,
            "failure_type": "test",
            "repair_attempt": 1,
        }
    )

    result = task.execute(stage)

    assert result.status == WorkflowStatus.TERMINAL
    error_msg = result.context.get("error", "")
    assert "permanently" in error_msg.lower() or "failed" in error_msg.lower()


@patch("trust5.tasks.repair_task.emit")
def test_repair_skips_when_no_test_output(mock_emit):
    """When test_output is empty and failure_type is not quality, skip repair
    but jump back to validate so downstream stages are unblocked."""
    task = RepairTask()
    stage = make_stage(
        {
            "_repair_requested": True,
            "test_output": "",
            "tests_passed": False,
            "tests_partial": False,
            "failure_type": "test",
        }
    )

    result = task.execute(stage)

    assert result.status == WorkflowStatus.REDIRECT
    assert result.target_stage_ref_id == "validate"
    assert result.outputs["repair_skipped"] is True


# ── _repair_requested crash recovery (get vs pop) ─────────────────────────


@patch("trust5.tasks.repair_task.emit")
def test_repair_requested_survives_reread(mock_emit):
    """_repair_requested uses get() not pop(), so it survives re-reads (crash recovery)."""
    task = RepairTask()
    stage = make_stage(
        {
            "_repair_requested": False,
            "test_output": "FAILED",
            "tests_passed": False,
        }
    )

    # Execute once — should skip (not requested)
    task.execute(stage)
    # Key should still be in context (get, not pop)
    assert "_repair_requested" in stage.context


@patch("trust5.tasks.repair_task.emit")
@patch("trust5.tasks.repair_task.Agent")
@patch("trust5.tasks.repair_task.LLM")
@patch("trust5.tasks.repair_task.summarize_errors", return_value="errors")
@patch("trust5.tasks.repair_task.build_repair_prompt", return_value="fix")
@patch("trust5.tasks.repair_task.propagate_context")
def test_repair_agent_gets_denied_test_files(
    mock_propagate, mock_prompt, mock_summarize, mock_llm_cls, mock_agent_cls, mock_emit
):
    """Repair agent is constructed with test_files as denied_files."""
    mock_agent = MagicMock()
    mock_agent.run.return_value = "done"
    mock_agent_cls.return_value = mock_agent
    mock_llm_cls.for_tier.return_value = MagicMock()

    task = RepairTask()
    stage = make_stage(
        {
            "_repair_requested": True,
            "test_output": "FAILED",
            "tests_passed": False,
            "tests_partial": False,
            "failure_type": "test",
            "repair_attempt": 1,
            "test_files": ["tests/test_core.py", "tests/test_utils.py"],
            "owned_files": ["src/core.py"],
        }
    )

    task.execute(stage)

    # Verify Agent was created with denied_files=test_files
    call_kwargs = mock_agent_cls.call_args[1]
    assert call_kwargs["denied_files"] == ["tests/test_core.py", "tests/test_utils.py"]
    assert call_kwargs["deny_test_patterns"] is True
    assert call_kwargs["owned_files"] == ["src/core.py"]


@patch("trust5.tasks.repair_task.emit")
@patch("trust5.tasks.repair_task.Agent")
@patch("trust5.tasks.repair_task.LLM")
@patch("trust5.tasks.repair_task.summarize_errors", return_value="quality issues")
@patch("trust5.tasks.repair_task.build_repair_prompt", return_value="fix quality")
@patch("trust5.tasks.repair_task.propagate_context")
def test_quality_repair_propagates_test_files(
    mock_propagate, mock_prompt, mock_summarize, mock_llm_cls, mock_agent_cls, mock_emit
):
    """Quality->repair path propagates test_files via propagate_context."""
    mock_agent = MagicMock()
    mock_agent.run.return_value = "Fixed lint"
    mock_agent_cls.return_value = mock_agent
    mock_llm_cls.for_tier.return_value = MagicMock()

    task = RepairTask()
    stage = make_stage(
        {
            "_repair_requested": True,
            "test_output": "QUALITY GATE FAILED",
            "tests_passed": True,  # quality failures can have passing tests
            "tests_partial": False,
            "failure_type": "quality",
            "repair_attempt": 1,
            "test_files": ["test_core.py"],
            "quality_attempt": 1,
            "max_quality_attempts": 3,
        }
    )

    task.execute(stage)

    # propagate_context should carry test_files to the quality jump context
    mock_propagate.assert_called()
    source_ctx = mock_propagate.call_args[0][0]
    assert source_ctx.get("test_files") == ["test_core.py"]


@patch("trust5.tasks.repair_task.emit")
@patch("trust5.tasks.repair_task.Agent")
@patch("trust5.tasks.repair_task.LLM")
@patch("trust5.tasks.repair_task.summarize_errors", return_value="errors")
@patch("trust5.tasks.repair_task.build_repair_prompt", return_value="fix")
@patch("trust5.tasks.repair_task.propagate_context")
def test_repair_rstr_includes_module_name(
    mock_propagate, mock_prompt, mock_summarize, mock_llm_cls, mock_agent_cls, mock_emit
):
    """RSTR emission includes [module_name] when present."""
    mock_agent = MagicMock()
    mock_agent.run.return_value = "done"
    mock_agent_cls.return_value = mock_agent
    mock_llm_cls.for_tier.return_value = MagicMock()

    task = RepairTask()
    stage = make_stage(
        {
            "_repair_requested": True,
            "test_output": "FAILED",
            "tests_passed": False,
            "tests_partial": False,
            "failure_type": "test",
            "repair_attempt": 1,
            "module_name": "api",
        }
    )

    task.execute(stage)

    from trust5.core.message import M

    rstr_calls = [call for call in mock_emit.call_args_list if call.args and call.args[0] == M.RSTR]
    assert rstr_calls, "Expected RSTR emission"
    assert "[api]" in rstr_calls[0].args[1]


# ── Regression: repair must always jump to validate (never short-circuit) ──


@patch("trust5.tasks.repair_task.emit")
@patch("trust5.tasks.repair_task.Agent")
@patch("trust5.tasks.repair_task.LLM")
@patch("trust5.tasks.repair_task.summarize_errors", return_value="errors")
@patch("trust5.tasks.repair_task.build_repair_prompt", return_value="fix")
@patch("trust5.tasks.repair_task.propagate_context")
def test_repair_always_jumps_to_validate_even_when_tests_pass(
    mock_propagate, mock_prompt, mock_summarize, mock_llm_cls, mock_agent_cls, mock_emit
):
    """Repair must ALWAYS jump_to validate — never return success directly.

    Returning TaskResult.success() from repair leaves the source stage
    (validate) without a normal CompleteStageHandler completion, which
    means downstream stages in a parallel pipeline are never unblocked.
    Regression test for: 60-minute hang in parallel pipeline.
    """
    mock_agent = MagicMock()
    mock_agent.run.return_value = "Fixed the code, all tests pass now"
    mock_agent_cls.return_value = mock_agent
    mock_llm_cls.for_tier.return_value = MagicMock()

    task = RepairTask()
    stage = make_stage(
        {
            "_repair_requested": True,
            "test_output": "FAILED test_merge - AssertionError",
            "tests_passed": False,
            "tests_partial": False,
            "failure_type": "test",
            "repair_attempt": 1,
            "jump_validate_ref": "validate_core",
        }
    )

    result = task.execute(stage)

    # Must be REDIRECT (jump_to), never SUCCEEDED
    assert result.status == WorkflowStatus.REDIRECT, (
        "Repair must jump to validate, not return success. "
        "Returning success from repair leaves downstream stages deadlocked."
    )
    assert result.target_stage_ref_id == "validate_core"


# ── Runtime "unknown" language re-detection (repair) ───────────────────────


@patch("trust5.tasks.repair_task.emit")
@patch("trust5.tasks.repair_task.detect_language", return_value="python")
@patch("trust5.tasks.repair_task.get_profile")
def test_repair_redetects_unknown_language(mock_get_profile, mock_detect, mock_emit):
    """When language_profile says 'unknown' but detect_language returns python,
    repair should update the profile before proceeding.

    Uses _repair_requested=False to exercise re-detection without needing
    to mock the full agent/LLM chain — re-detection runs before the skip check.
    """
    python_dict = {
        "language": "python",
        "extensions": (".py",),
        "test_command": ("python3", "-m", "pytest", "-v", "--tb=long", "-x"),
        "test_verify_command": 'Bash("pytest -v --tb=short")',
        "syntax_check_command": ("python3", "-m", "compileall", "-q", "."),
        "skip_dirs": ("__pycache__", ".venv", "venv"),
    }
    mock_profile_obj = MagicMock()
    mock_profile_obj.to_dict.return_value = python_dict
    mock_get_profile.return_value = mock_profile_obj

    task = RepairTask()
    unknown_profile = {
        "language": "unknown",
        "extensions": (),
        "test_command": ("echo", "no default test command"),
        "test_verify_command": "echo 'no tests'",
        "syntax_check_command": None,
        "skip_dirs": (".moai", ".trust5", ".git"),
    }
    stage = make_stage(
        {
            # _repair_requested=False triggers early skip, but re-detection runs first
            "_repair_requested": False,
            "language_profile": unknown_profile,
        }
    )

    task.execute(stage)

    # Language profile should have been updated in context before skip
    assert stage.context["language_profile"]["language"] == "python"
    mock_detect.assert_called_once()
    mock_get_profile.assert_called_with("python")


@patch("trust5.tasks.repair_task.emit")
@patch("trust5.tasks.repair_task.detect_language", return_value="unknown")
def test_repair_keeps_unknown_when_detection_fails(mock_detect, mock_emit):
    """When detect_language also returns 'unknown', profile stays unchanged."""
    task = RepairTask()
    unknown_profile = {
        "language": "unknown",
        "extensions": (),
        "test_command": ("echo", "no default test command"),
        "test_verify_command": "echo 'no tests'",
        "syntax_check_command": None,
        "skip_dirs": (".moai", ".trust5", ".git"),
    }
    stage = make_stage(
        {
            "_repair_requested": False,  # Will skip early
            "language_profile": unknown_profile,
        }
    )

    task.execute(stage)

    # Profile should remain unknown since detect_language also returned unknown
    assert stage.context["language_profile"]["language"] == "unknown"


# ── Repair pre-flight uses plan_config test_command ────────────────────────


@patch("trust5.tasks.repair_task.emit")
@patch("trust5.tasks.repair_task.subprocess.run")
def test_repair_preflight_uses_plan_config_test_command(mock_run, mock_emit):
    """Pre-flight check should use plan_config.test_command when available,
    matching ValidateTask behavior. This prevents false positives when the
    profile test_command is 'echo no default'."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_run.return_value = mock_result

    task = RepairTask()
    stage = make_stage(
        {
            "_repair_requested": True,
            "test_output": "FAILED",
            "tests_passed": False,
            "tests_partial": False,
            "failure_type": "test",
            "repair_attempt": 1,
            "plan_config": {"test_command": ". venv/bin/activate && pytest"},
            "language_profile": {
                "language": "python",
                "extensions": (".py",),
                "test_command": ("python3", "-m", "pytest", "-v", "--tb=long", "-x"),
                "test_verify_command": 'Bash("pytest -v --tb=short")',
                "syntax_check_command": None,
                "skip_dirs": (),
            },
        }
    )

    # Pre-flight will run and find tests pass → jump to validate
    result = task.execute(stage)

    assert result.status == WorkflowStatus.REDIRECT
    assert result.target_stage_ref_id == "validate"
    # Verify subprocess was called with sh -c wrapping (plan_config has &&)
    call_args = mock_run.call_args
    assert call_args[0][0] == ["sh", "-c", ". venv/bin/activate && pytest", "--timeout=30"]


# ── Regression: pre-flight must NOT run for lint/syntax failures ──────────


@patch("trust5.tasks.repair_task.emit")
@patch("trust5.tasks.repair_task.Agent")
@patch("trust5.tasks.repair_task.LLM")
@patch("trust5.tasks.repair_task.summarize_errors", return_value="lint errors")
@patch("trust5.tasks.repair_task.build_repair_prompt", return_value="fix lint")
@patch("trust5.tasks.repair_task.propagate_context")
def test_repair_no_preflight_for_lint_failure(
    mock_propagate, mock_prompt, mock_summarize, mock_llm_cls, mock_agent_cls, mock_emit
):
    """Lint failures must NOT trigger pre-flight test check.

    Regression test for infinite loop: validate(lint fail) → repair(test pass
    pre-flight → skip) → validate(lint fail) → repeat until jump limit.
    """
    mock_agent = MagicMock()
    mock_agent.run.return_value = "Fixed lint issues"
    mock_agent_cls.return_value = mock_agent
    mock_llm_cls.for_tier.return_value = MagicMock()

    task = RepairTask()
    stage = make_stage(
        {
            "_repair_requested": True,
            "test_output": "Lint check failed: No module named ruff",
            "tests_passed": False,
            "tests_partial": False,
            "failure_type": "lint",
            "repair_attempt": 1,
        }
    )

    with patch("trust5.tasks.repair_task.subprocess.run") as mock_subprocess:
        result = task.execute(stage)

        # subprocess should NOT be called (no pre-flight for lint failures)
        mock_subprocess.assert_not_called()

    # Repair agent should have been invoked
    mock_agent.run.assert_called_once()
    # Should jump to validate after repair
    assert result.status == WorkflowStatus.REDIRECT
    assert result.target_stage_ref_id == "validate"


@patch("trust5.tasks.repair_task.emit")
@patch("trust5.tasks.repair_task.Agent")
@patch("trust5.tasks.repair_task.LLM")
@patch("trust5.tasks.repair_task.summarize_errors", return_value="syntax error")
@patch("trust5.tasks.repair_task.build_repair_prompt", return_value="fix syntax")
@patch("trust5.tasks.repair_task.propagate_context")
def test_repair_no_preflight_for_syntax_failure(
    mock_propagate, mock_prompt, mock_summarize, mock_llm_cls, mock_agent_cls, mock_emit
):
    """Syntax failures must NOT trigger pre-flight test check."""
    mock_agent = MagicMock()
    mock_agent.run.return_value = "Fixed syntax error"
    mock_agent_cls.return_value = mock_agent
    mock_llm_cls.for_tier.return_value = MagicMock()

    task = RepairTask()
    stage = make_stage(
        {
            "_repair_requested": True,
            "test_output": "Syntax check failed: invalid syntax at line 42",
            "tests_passed": False,
            "tests_partial": False,
            "failure_type": "syntax",
            "repair_attempt": 1,
        }
    )

    with patch("trust5.tasks.repair_task.subprocess.run") as mock_subprocess:
        result = task.execute(stage)

        # subprocess should NOT be called (no pre-flight for syntax failures)
        mock_subprocess.assert_not_called()

    # Repair agent should have been invoked
    mock_agent.run.assert_called_once()
    assert result.status == WorkflowStatus.REDIRECT


# ── Plan_config verify_cmd propagation ─────────────────────────────────────


@patch("trust5.tasks.repair_task.emit")
@patch("trust5.tasks.repair_task.Agent")
@patch("trust5.tasks.repair_task.LLM")
@patch("trust5.tasks.repair_task.summarize_errors", return_value="test failure")
@patch("trust5.tasks.repair_task.propagate_context")
def test_repair_passes_plan_config_to_build_repair_prompt(
    mock_propagate, mock_summarize, mock_llm_cls, mock_agent_cls, mock_emit
):
    """RepairTask passes plan_config to build_repair_prompt so the repairer
    gets the venv-activated test command instead of the bare profile default."""
    mock_agent = MagicMock()
    mock_agent.run.return_value = "Fixed the bug"
    mock_agent_cls.return_value = mock_agent
    mock_llm_cls.for_tier.return_value = MagicMock()

    plan_config = {
        "test_command": "source venv/bin/activate && python -m pytest tests/ -v",
    }
    task = RepairTask()
    stage = make_stage(
        {
            "_repair_requested": True,
            "test_output": "FAILED test_something",
            "tests_passed": False,
            "failure_type": "test",
            "repair_attempt": 1,
            "plan_config": plan_config,
        }
    )

    with patch("trust5.tasks.repair_task.build_repair_prompt", return_value="fix it") as mock_build:
        with patch("trust5.tasks.repair_task.subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(returncode=1)  # pre-flight fails
            result = task.execute(stage)

        # build_repair_prompt must receive the plan_config
        call_kwargs = mock_build.call_args
        assert call_kwargs[1].get("plan_config") == plan_config or (
            len(call_kwargs[0]) >= 8 and call_kwargs[0][7] == plan_config
        )

    assert result.status == WorkflowStatus.REDIRECT


# ── build_repair_prompt uses plan_config test_command ──────────────────────


# ── _build_cross_module_hint tests ──────────────────────────────────────────


class TestBuildCrossModuleHint:
    """Tests for _build_cross_module_hint() which detects interface mismatches."""

    def test_returns_empty_for_no_output(self):
        from trust5.tasks.repair_task import _build_cross_module_hint

        assert _build_cross_module_hint("", ["src/core.py"]) == ""

    def test_detects_typeerror_with_argument(self):
        from trust5.tasks.repair_task import _build_cross_module_hint

        output = "TypeError: Worker.__init__() got an unexpected keyword argument 'queue'"
        result = _build_cross_module_hint(output, ["celerylited/worker.py"])
        assert "Cross-Module Interface Mismatch" in result
        assert "celerylited/worker.py" in result

    def test_detects_typeerror_missing_positional(self):
        from trust5.tasks.repair_task import _build_cross_module_hint

        output = "TypeError: __init__() missing 1 required positional argument: 'db_path'"
        result = _build_cross_module_hint(output, ["src/broker.py"])
        assert "Cross-Module Interface Mismatch" in result

    def test_detects_attributeerror(self):
        from trust5.tasks.repair_task import _build_cross_module_hint

        output = "AttributeError: 'CeleryLite' object has no attribute 'task'"
        result = _build_cross_module_hint(output, ["src/app.py"])
        assert "Cross-Module Interface Mismatch" in result
        assert "READ the failing test files" in result

    def test_detects_importerror(self):
        from trust5.tasks.repair_task import _build_cross_module_hint

        output = "ImportError: cannot import name 'TaskWrapper' from 'celerylited.task'"
        result = _build_cross_module_hint(output, ["celerylited/task.py"])
        assert "Cross-Module Interface Mismatch" in result

    def test_ignores_assertion_errors(self):
        from trust5.tasks.repair_task import _build_cross_module_hint

        output = "AssertionError: expected 42 but got 0"
        result = _build_cross_module_hint(output, ["src/core.py"])
        assert result == ""

    def test_ignores_plain_test_failures(self):
        from trust5.tasks.repair_task import _build_cross_module_hint

        output = "FAILED test_add - assert 2 + 2 == 5"
        result = _build_cross_module_hint(output, ["src/math.py"])
        assert result == ""


def test_build_repair_prompt_uses_plan_config_test_command():
    """When plan_config has test_command, it overrides the profile test_verify_command."""
    from trust5.core.context_builder import build_repair_prompt

    plan_config = {
        "test_command": "source venv/bin/activate && python -m pytest tests/ -v",
    }
    language_profile = {
        "test_verify_command": 'Bash("pytest -v --tb=short")',
        "extensions": (".py",),
        "skip_dirs": ("__pycache__",),
    }
    prompt = build_repair_prompt(
        test_output="FAILED",
        project_root="/tmp/proj",
        language_profile=language_profile,
        plan_config=plan_config,
    )
    # The venv-activated command should appear in the repair prompt
    assert "source venv/bin/activate" in prompt
    # The bare profile default should NOT be the verify command
    assert 'Bash("pytest -v --tb=short")' not in prompt
