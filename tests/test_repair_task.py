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
