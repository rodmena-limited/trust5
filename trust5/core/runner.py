from __future__ import annotations
import signal
import sys
import threading
import time
from typing import Any
from stabilize import Orchestrator, QueueProcessor, SqliteWorkflowStore, Workflow
from stabilize.models.status import WorkflowStatus
from .message import M, emit
TERMINAL_STATUSES: frozenset[WorkflowStatus] = frozenset(
    {
        WorkflowStatus.SUCCEEDED,
        WorkflowStatus.FAILED_CONTINUE,
        WorkflowStatus.TERMINAL,
        WorkflowStatus.CANCELED,
    }
)
POLL_INTERVAL: float = 0.5

def check_stage_failures(workflow: Workflow) -> tuple[bool, bool, bool, list[str]]:
    """Inspect stage outputs for test/quality/compliance failures hidden behind SUCCEEDED.

    Stabilize rolls up FAILED_CONTINUE stages to SUCCEEDED at workflow level
    (by design). We must check individual stage outputs to detect partial failures.

    Returns:
        (has_test_failures, has_quality_failure, has_compliance_failure, detail_messages)
    """
    has_test_failures = False
    has_quality_failure = False
    has_compliance_failure = False
    details: list[str] = []

    for stage in workflow.stages:
        outputs = stage.outputs or {}

        # Check compliance on any stage that has compliance data (even SUCCEEDED)
        spec_ratio = outputs.get("spec_compliance_ratio")
        if spec_ratio is not None:
            try:
                ratio = float(spec_ratio)
                if ratio < 1.0:
                    met = outputs.get("spec_criteria_met", "?")
                    total = outputs.get("spec_criteria_total", "?")
                    unmet = outputs.get("spec_unmet_criteria", [])
                    details.append(
                        f"  - Stage '{stage.ref_id}': SPEC compliance {met}/{total} "
                        f"(ratio: {ratio:.2f})"
                    )
                    for uc in unmet[:5]:
                        details.append(f"      {uc}")
                    if ratio < 0.7:
                        has_compliance_failure = True
            except (ValueError, TypeError):
                pass

        if stage.status not in (WorkflowStatus.FAILED_CONTINUE, WorkflowStatus.TERMINAL):
            continue

        if outputs.get("tests_passed") is False or outputs.get("tests_partial"):
            has_test_failures = True
            attempts = outputs.get("repair_attempts_used", "?")
            details.append(f"  - Stage '{stage.ref_id}': tests failing (repair attempts: {attempts})")
        # TERMINAL stages from exhausted reimplementations also indicate test failures
        if stage.status == WorkflowStatus.TERMINAL:
            error = getattr(stage, "error", "") or ""
            if "tests still failing" in error.lower() or "reimplementation" in error.lower():
                has_test_failures = True
                details.append(f"  - Stage '{stage.ref_id}': {error[:200]}")
        if outputs.get("quality_passed") is False:
            has_quality_failure = True
            score = outputs.get("quality_score", "?")
            details.append(f"  - Stage '{stage.ref_id}': quality gate failed (score: {score})")

    return has_test_failures, has_quality_failure, has_compliance_failure, details

def finalize_status(
    result: Workflow,
    store: SqliteWorkflowStore,
    prefix: str = "Status",
) -> None:
    """Check stage-level failures hidden behind SUCCEEDED.

    Only TEST failures override to TERMINAL (code is broken, needs resume).
    Quality-only failures keep SUCCEEDED with a warning — the code works,
    quality is advisory.
    """
    status_name = result.status.name if hasattr(result.status, "name") else str(result.status)

    if status_name in ("SUCCEEDED", "COMPLETED"):
        has_test_fail, has_quality_fail, has_compliance_fail, details = check_stage_failures(result)

        if has_test_fail:
            # Tests failing = code is broken → TERMINAL (resumable)
            result.status = WorkflowStatus.TERMINAL
            store.update_status(result)

            problems = ["tests failing"]
            if has_quality_fail:
                problems.append("quality gate failed")
            if has_compliance_fail:
                problems.append("SPEC compliance below threshold")
            emit(M.WFAL, f"{prefix}: FAILED ({', '.join(problems)})")
            for detail in details:
                emit(M.WFAL, detail)
            emit(M.WFAL, "Run 'trust5 resume' to retry from the failed stage.")
        elif has_compliance_fail:
            # SPEC compliance failed but tests pass → code works but is incomplete
            warnings = ["SPEC compliance below threshold"]
            if has_quality_fail:
                warnings.append("quality gate failed")
            emit(M.WSUC, f"{prefix}: {status_name} (with SPEC compliance warnings)")
            emit(M.SWRN, "SPEC COMPLIANCE WARNING — the following criteria are not addressed:")
            for detail in details:
                emit(M.SWRN, detail)
            emit(M.SWRN, "The code works but may be missing features from the SPEC.")
        elif has_quality_fail:
            # Quality failed but tests pass → code works, warn but keep SUCCEEDED
            emit(M.WSUC, f"{prefix}: {status_name} (with quality warnings)")
            for detail in details:
                emit(M.SWRN, detail)
            emit(M.SWRN, "Quality gate did not pass. Run 'trust5 loop' to improve.")
        else:
            emit(M.WSUC, f"{prefix}: {status_name}")
    elif status_name in ("FAILED_CONTINUE",):
        emit(M.WFAL, f"{prefix}: FAILED (incomplete)")
    else:
        emit(M.WFAL, f"{prefix}: {status_name}")
