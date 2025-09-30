from __future__ import annotations
import threading
from unittest.mock import MagicMock, patch
from stabilize.models.status import WorkflowStatus
from trust5.core.runner import check_stage_failures, finalize_status, wait_for_completion

def make_stage(ref_id: str, status: WorkflowStatus, outputs: dict | None = None, error: str | None = None):
    stage = MagicMock()
    stage.ref_id = ref_id
    stage.status = status
    stage.outputs = outputs or {}
    stage.error = error or ""
    return stage

def make_workflow(status: WorkflowStatus, stages: list) -> MagicMock:
    workflow = MagicMock()
    workflow.status = status
    workflow.stages = stages
    return workflow

def test_check_stage_failures_detects_test_failure():
    """FAILED_CONTINUE stage with tests_passed=False is detected."""
    stages = [
        make_stage("setup", WorkflowStatus.SUCCEEDED),
        make_stage("validate", WorkflowStatus.FAILED_CONTINUE, {"tests_passed": False, "repair_attempts_used": 5}),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)

    has_test, has_quality, has_compliance, details = check_stage_failures(workflow)

    assert has_test is True
    assert has_quality is False
    assert len(details) >= 1
    assert "tests failing" in details[0].lower()

def test_check_stage_failures_detects_quality_failure():
    """FAILED_CONTINUE stage with quality_passed=False is detected."""
    stages = [
        make_stage("setup", WorkflowStatus.SUCCEEDED),
        make_stage("quality", WorkflowStatus.FAILED_CONTINUE, {"quality_passed": False, "quality_score": 0.55}),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)

    has_test, has_quality, has_compliance, details = check_stage_failures(workflow)

    assert has_test is False
    assert has_quality is True
    assert len(details) >= 1
    assert "quality" in details[0].lower()
