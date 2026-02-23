"""Workflow execution helpers extracted from main.py."""

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

POLL_INTERVAL_FAST: float = 0.5  # first 60 seconds
POLL_INTERVAL_MODERATE: float = 2.0  # 1-5 minutes
POLL_INTERVAL_SLOW: float = 5.0  # beyond 5 minutes
POLL_INTERVAL: float = POLL_INTERVAL_FAST  # backward compat alias


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
                    details.append(f"  - Stage '{stage.ref_id}': SPEC compliance {met}/{total} (ratio: {ratio:.2f})")
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
        if outputs.get("review_passed") is False:
            # Review failures are advisory (same as quality) — warn but don't override to TERMINAL
            has_quality_failure = True
            r_score = outputs.get("review_score", "?")
            details.append(f"  - Stage '{stage.ref_id}': code review failed (score: {r_score})")

    return has_test_failures, has_quality_failure, has_compliance_failure, details


def finalize_status(
    result: Workflow,
    store: SqliteWorkflowStore,
    prefix: str = "Status",
) -> None:
    """Check stage-level failures hidden behind SUCCEEDED.
    TEST failures and SPEC COMPLIANCE failures override to TERMINAL (resumable).
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
            # SPEC compliance failed → code works but is incomplete → TERMINAL (resumable)
            result.status = WorkflowStatus.TERMINAL
            store.update_status(result)

            problems = ["SPEC compliance below threshold"]
            if has_quality_fail:
                problems.append("quality gate failed")
            emit(M.WFAL, f"{prefix}: FAILED ({', '.join(problems)})")
            emit(M.WFAL, "SPEC COMPLIANCE FAILURE — the following criteria are not addressed:")
            for detail in details:
                emit(M.WFAL, detail)
            emit(M.WFAL, "Run 'trust5 resume' to retry from the failed stage.")
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


def wait_for_completion(
    store: SqliteWorkflowStore,
    workflow_id: str,
    timeout: float,
    stop_event: threading.Event | None = None,
) -> Workflow:
    """Poll workflow status until it reaches a terminal state, timeout, or stop signal.
    Uses progressive polling intervals to reduce DB load on long-running pipelines:
    - First 60s: 0.5s (fast feedback during startup)
    - 1-5 min: 2.0s (moderate)
    - Beyond 5 min: 5.0s (minimal DB overhead for multi-day runs)
    When *stop_event* is provided and becomes set (e.g. TUI exits via Ctrl+C),
    the poll loop exits immediately so the calling thread can clean up its
    QueueProcessor and other resources without blocking for the full timeout.
    """
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


def run_workflow(
    processor: QueueProcessor,
    orchestrator: Orchestrator,
    store: SqliteWorkflowStore,
    workflow: Workflow,
    timeout: float,
    label: str,
    db_path: str = "",
) -> Workflow:
    """Submit, execute, and finalize a workflow with SIGINT handling.
    Stores the workflow, starts orchestration and queue processing,
    waits for completion (with progressive polling), and calls
    ``finalize_status`` to detect hidden stage failures.
    """
    store.store(workflow)
    orchestrator.start(workflow)

    emit(M.WSTR, f"{label} started: {workflow.id}")

    def handle_signal(sig: int, frame: Any) -> None:
        emit(M.WINT, f"Interrupted. Workflow {workflow.id} state preserved in {db_path}.")
        processor.request_stop()
        processor.stop(wait=False)
        sys.exit(130)

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
        except Exception as e:
            emit(M.SERR, f"Force-cancel failed: {e}. Marking TERMINAL directly.")
            result.status = WorkflowStatus.TERMINAL
            store.update_status(result)

        result = store.retrieve(workflow.id)

    finalize_status(result, store, prefix="Status")
    return result
