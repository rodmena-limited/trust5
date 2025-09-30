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
