"""Workflow execution helpers extracted from main.py."""

from __future__ import annotations

import logging
import os
import signal
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

POLL_INTERVAL_FAST: float = 0.5
POLL_INTERVAL_MODERATE: float = 2.0
POLL_INTERVAL_SLOW: float = 5.0
POLL_INTERVAL: float = POLL_INTERVAL_FAST


def check_stage_failures(workflow: Workflow) -> tuple[bool, bool, bool, bool, list[str]]:
    """Inspect stage outputs for test/quality/review/compliance failures.

    Returns:
        (has_test_failures, has_quality_failure, has_review_failure,
         has_compliance_failure, detail_messages)
    """
    has_test_failures = False
    has_quality_failure = False
    has_review_failure = False
    has_compliance_failure = False
    details: list[str] = []

    for stage in workflow.stages:
        outputs = stage.outputs or {}

        # --- SPEC compliance reporting & failure detection ---
        spec_ratio = outputs.get("spec_compliance_ratio")
        if spec_ratio is not None:
            try:
                ratio = float(spec_ratio)
                if ratio < 1.0:
                    met = outputs.get("spec_criteria_met", "?")
                    total = outputs.get("spec_criteria_total", "?")
                    unmet = outputs.get("spec_unmet_criteria", [])
                    details.append(f"  - Stage '{stage.ref_id}': SPEC compliance {met}/{total} (ratio: {ratio:.2f})")
                    for uc in unmet[:5]:
                        details.append(f"      {uc}")
                if outputs.get("compliance_passed") is False:
                    has_compliance_failure = True
            except (ValueError, TypeError):
                pass

        # --- Test failure detection (only for failed/terminal stages) ---
        if stage.status not in (WorkflowStatus.FAILED_CONTINUE, WorkflowStatus.TERMINAL):
            continue

        if outputs.get("tests_passed") is False or outputs.get("tests_partial") is True:
            has_test_failures = True
            attempts = outputs.get("repair_attempts_used", "?")
            details.append(f"  - Stage '{stage.ref_id}': tests failing (repair attempts: {attempts})")

        if stage.status == WorkflowStatus.TERMINAL:
            error = getattr(stage, "error", "") or ""
            if "tests still failing" in error.lower() or "reimplementation" in error.lower():
                has_test_failures = True
                details.append(f"  - Stage '{stage.ref_id}': {error[:200]}")

        # --- Quality & review failure detection ---
        if outputs.get("quality_passed") is False:
            has_quality_failure = True
            score = outputs.get("quality_score", "?")
            details.append(f"  - Stage '{stage.ref_id}': quality gate failed (score: {score})")

        # Review failures only count if NOT advisory mode
        # Advisory mode (review_advisory=True) means "inform but don't block"
        if outputs.get("review_passed") is False and not outputs.get("review_advisory"):
            has_review_failure = True
            r_score = outputs.get("review_score", "?")
            details.append(f"  - Stage '{stage.ref_id}': code review failed (score: {r_score})")

    return has_test_failures, has_quality_failure, has_review_failure, has_compliance_failure, details


def finalize_status(
    result: Workflow,
    store: SqliteWorkflowStore,
    prefix: str = "Status",
) -> None:
    """Check stage-level failures hidden behind SUCCEEDED.

    ANY failure (test, quality, review, compliance) overrides to TERMINAL.
    A SUCCEEDED pipeline means ALL gates passed.
    """
    status_name = result.status.name if hasattr(result.status, "name") else str(result.status)

    if status_name in ("SUCCEEDED", "COMPLETED"):
        has_test_fail, has_quality_fail, has_review_fail, has_compliance_fail, details = check_stage_failures(result)
        any_failure = has_test_fail or has_quality_fail or has_review_fail or has_compliance_fail

        if any_failure:
            result.status = WorkflowStatus.TERMINAL
            store.update_status(result)

            problems: list[str] = []
            if has_test_fail:
                problems.append("tests failing")
            if has_quality_fail:
                problems.append("quality gate failed")
            if has_review_fail:
                problems.append("code review failed")
            if has_compliance_fail:
                problems.append("SPEC compliance below threshold")
            emit(M.WFAL, f"{prefix}: FAILED ({', '.join(problems)})")
            for detail in details:
                emit(M.WFAL, detail)
            if has_compliance_fail:
                emit(M.WFAL, "SPEC COMPLIANCE FAILURE \u2014 criteria not addressed in source code.")
            if has_test_fail or has_compliance_fail:
                emit(M.WFAL, "Run 'trust5 resume' to retry from the failed stage.")
            else:
                emit(M.WFAL, "Run 'trust5 loop' to address remaining issues.")
        else:
            emit(M.WSUC, f"{prefix}: {status_name}")
    elif status_name in ("FAILED_CONTINUE",):
        emit(M.WFAL, f"{prefix}: FAILED (incomplete)")
    else:
        emit(M.WFAL, f"{prefix}: {status_name}")


def wait_for_completion(
    store: SqliteWorkflowStore,
    workflow_id: str,
    timeout: float,
    stop_event: threading.Event | None = None,
) -> Workflow:
    """Poll workflow status until it reaches a terminal state, timeout, or stop signal."""
    start = time.monotonic()
    deadline = start + timeout
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return store.retrieve(workflow_id)
        result = store.retrieve(workflow_id)
        if result.status in TERMINAL_STATUSES:
            return result
        elapsed = time.monotonic() - start
        if elapsed < 60:
            interval = POLL_INTERVAL_FAST
        elif elapsed < 300:
            interval = POLL_INTERVAL_MODERATE
        else:
            interval = POLL_INTERVAL_SLOW
        time.sleep(interval)
    return store.retrieve(workflow_id)


logger = logging.getLogger(__name__)

# Stages that are considered 'failed' and need reset for auto-retry.
_FAILED_STAGE_STATUSES = frozenset(
    {
        WorkflowStatus.TERMINAL,
        WorkflowStatus.CANCELED,
        WorkflowStatus.FAILED_CONTINUE,
    }
)


def _reset_stage_for_retry(stage: Any) -> None:
    """Clear stale counters so a retried stage gets fresh attempts."""
    ctx = stage.context
    ctx.pop("quality_attempt", None)
    ctx.pop("prev_quality_report", None)
    ctx.pop("tests_partial", None)
    ctx.pop("previous_failures", None)
    ctx.pop("reimplementation_count", None)
    ctx.pop("diagnostic_baseline", None)
    ctx.pop("last_repair_summary", None)
    ctx.pop("_repair_requested", None)
    stage.outputs = {}
    stage.end_time = None
    stage.start_time = None


def reset_failed_stages(
    workflow: Workflow,
    store: SqliteWorkflowStore,
) -> int:
    """Reset TERMINAL/CANCELED/FAILED_CONTINUE stages to RUNNING for auto-retry.

    Returns the number of stages reset.  Also resets downstream SKIPPED/NOT_STARTED
    stages so the DAG can re-trigger them.
    """
    found_failed = False
    reset_count = 0
    downstream_count = 0

    for stage in workflow.stages:
        if stage.status in _FAILED_STAGE_STATUSES:
            found_failed = True
            reset_count += 1
            logger.info(
                "Auto-retry: resetting stage '%s' (%s -> RUNNING)",
                stage.ref_id,
                stage.status.name,
            )
            emit(
                M.WRCV,
                f"Auto-retry: resetting '{stage.ref_id}' ({stage.status.name} \u2192 RUNNING)",
            )
            _reset_stage_for_retry(stage)
            stage.status = WorkflowStatus.RUNNING
            for task in stage.tasks:
                if task.status in _FAILED_STAGE_STATUSES:
                    task.status = WorkflowStatus.RUNNING
            store.store_stage(stage)

        elif found_failed and stage.status in (
            WorkflowStatus.SKIPPED,
            WorkflowStatus.NOT_STARTED,
        ):
            downstream_count += 1
            stage.status = WorkflowStatus.NOT_STARTED
            stage.start_time = None
            stage.end_time = None
            for task in stage.tasks:
                task.status = WorkflowStatus.NOT_STARTED
                task.start_time = None
                task.end_time = None
            store.store_stage(stage)

    if reset_count > 0:
        workflow.status = WorkflowStatus.RUNNING
        workflow.end_time = None
        store.update_status(workflow)
        emit(
            M.WRCV,
            f"Auto-retry: reset {reset_count} failed + {downstream_count} downstream stage(s)",
        )

    return reset_count


def run_workflow(
    processor: QueueProcessor,
    orchestrator: Orchestrator,
    store: SqliteWorkflowStore,
    workflow: Workflow,
    timeout: float,
    label: str,
    db_path: str = "",
) -> Workflow:
    """Submit, execute, and finalize a workflow with SIGINT handling."""
    store.store(workflow)
    orchestrator.start(workflow)

    emit(M.WSTR, f"{label} started: {workflow.id}")

    def handle_signal(sig: int, frame: Any) -> None:
        emit(M.WINT, f"Interrupted. Workflow {workflow.id} state preserved in {db_path}.")
        processor.request_stop()
        processor.stop(wait=False)
        os._exit(130)

    original_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        processor.start()
        result = wait_for_completion(store, workflow.id, timeout)
    finally:
        signal.signal(signal.SIGINT, original_sigint)
        processor.request_stop()
        processor.stop(wait=False)

    if result.status == WorkflowStatus.RUNNING:
        emit(
            M.WTMO,
            f"Workflow {workflow.id} still RUNNING after timeout ({timeout:.0f}s). Force-canceling.",
        )
        try:
            orchestrator.cancel(result, user="trust5-timeout", reason=f"Timed out after {timeout}s")
            processor.process_all(timeout=30)
        except (OSError, RuntimeError) as e:  # cancel: orchestrator/DB errors
            emit(M.SERR, f"Force-cancel failed: {e}. Marking TERMINAL directly.")
            result.status = WorkflowStatus.TERMINAL
            store.update_status(result)

        result = store.retrieve(workflow.id)

    finalize_status(result, store, prefix="Status")
    return result
