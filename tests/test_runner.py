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
