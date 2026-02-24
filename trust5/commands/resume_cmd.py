"""Resume command logic for Trust5 pipeline.

Provides the ability to resume a TERMINAL/CANCELED pipeline from its
failed stage, preserving context and using stabilize's recovery mechanism.
"""

from __future__ import annotations

import os
import signal
from typing import Any

import typer
from stabilize.models.status import WorkflowStatus
from stabilize.recovery import recover_on_startup

from ..core.constants import TIMEOUT_DEVELOP as _TIMEOUT_DEVELOP
from ..core.message import M, emit
from ..core.runner import finalize_status, wait_for_completion
from ..core.tools import Tools
from ..infrastructure import setup_stabilize
from ..tui_runner import (
    _print_final_summary,
    _restore_stdout_after_tui,
    _wait_with_tui,
)


def _reset_stage_for_resume(stage: Any) -> None:
    """Clear stale counters so a resumed stage gets fresh attempts."""
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


def resume_logic(use_tui: bool) -> None:
    """Resume the last TERMINAL pipeline from its failed stage.

    Uses stabilize's public API exclusively:
    - retrieve_by_application() to find TERMINAL workflows
    - Workflow/StageExecution/TaskExecution model objects for status changes
    - store.store() to persist modified workflow
    - recover_on_startup() to re-queue recovery messages

    Context preservation strategy:
    - Failed stages (TERMINAL/CANCELED) -> RUNNING: recovery sends RunTask,
      which reuses existing stage context (no amnesia).
    - Downstream stages (SKIPPED/NOT_STARTED after a failure) -> NOT_STARTED:
      DAG-triggered by upstream completion via StartStage.
    """
    from stabilize.persistence.store import WorkflowCriteria

    Tools.set_non_interactive(True)
    processor, _orchestrator, store, queue, db_path = setup_stabilize(use_tui=use_tui)

    # ── Find latest resumable workflow via public API ──
    # FAILED_CONTINUE at workflow level shouldn't happen (finalize_status
    # overrides to TERMINAL), but include it defensively.
    # RUNNING is included for hard-kill recovery (process killed, workflow
    # left RUNNING in DB with no active processor).
    resumable_wf = {
        WorkflowStatus.TERMINAL,
        WorkflowStatus.CANCELED,
        WorkflowStatus.FAILED_CONTINUE,
        WorkflowStatus.RUNNING,
    }
    criteria = WorkflowCriteria(statuses=resumable_wf, page_size=10)

    target_wf = None
    for wf in store.retrieve_by_application("trust5", criteria):
        if target_wf is None or (wf.start_time or 0) > (target_wf.start_time or 0):
            target_wf = wf
    if target_wf is None:
        emit(M.SWRN, "No resumable pipeline found. Nothing to resume.")
        raise typer.Exit(1)

    workflow = store.retrieve(target_wf.id)
    old_status = workflow.status.name
    emit(M.WRCV, f"Found {old_status} pipeline: {workflow.id}")

    # ── Reset statuses through model objects ──
    # Stage-level: TERMINAL/CANCELED/FAILED_CONTINUE/RUNNING all count as
    # "needs resume" (RUNNING = stage was mid-execution when process died)
    resumable_stages = {
        WorkflowStatus.TERMINAL,
        WorkflowStatus.CANCELED,
        WorkflowStatus.FAILED_CONTINUE,
        WorkflowStatus.RUNNING,
    }
    found_failed = False
    failed_count = 0
    downstream_count = 0

    for stage in workflow.stages:
        if stage.status in resumable_stages:
            found_failed = True
            failed_count += 1
            emit(
                M.WRCV,
                f"  Resuming stage '{stage.ref_id}' ({stage.status.name} -> RUNNING, context preserved)",
            )
            _reset_stage_for_resume(stage)
            stage.status = WorkflowStatus.RUNNING
            for task in stage.tasks:
                if task.status in resumable_stages:
                    task.status = WorkflowStatus.RUNNING

        elif found_failed and stage.status in (
            WorkflowStatus.SKIPPED,
            WorkflowStatus.NOT_STARTED,
        ):
            downstream_count += 1
            emit(M.WRCV, f"  Resetting downstream '{stage.ref_id}' -> NOT_STARTED")
            stage.status = WorkflowStatus.NOT_STARTED
            stage.start_time = None
            stage.end_time = None
            for task in stage.tasks:
                task.status = WorkflowStatus.NOT_STARTED
                task.start_time = None
                task.end_time = None

    if failed_count == 0:
        emit(M.SWRN, "No failed stages found in the pipeline. Nothing to resume.")
        raise typer.Exit(1)

    workflow.status = WorkflowStatus.RUNNING
    workflow.end_time = None

    # ── Persist via public API ──
    store.update_status(workflow)
    for stage in workflow.stages:
        store.store_stage(stage)
    emit(
        M.WRCV,
        f"Pipeline {workflow.id} reset to RUNNING "
        f"({failed_count} failed, {downstream_count} downstream). "
        "Starting recovery...",
    )

    # ── Let stabilize's recovery re-queue messages ──
    recovered = recover_on_startup(store, queue, application="trust5")
    if recovered:
        emit(M.WRCV, f"Recovery queued {len(recovered)} workflow(s)")
    else:
        emit(
            M.SWRN,
            "Recovery found nothing to queue. The pipeline may need manual inspection.",
        )
        raise typer.Exit(1)

    emit(M.WSTR, f"Resuming pipeline {workflow.id}")

    def handle_signal(sig: int, frame: Any) -> None:
        emit(
            M.WINT,
            f"Interrupted. Workflow {workflow.id} state preserved in {db_path}.",
        )
        processor.request_stop()
        processor.stop(wait=False)
        os._exit(130)

    original_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handle_signal)

    changed_files: set[str] = set()
    try:
        processor.start()
        if use_tui:
            changed_files = _wait_with_tui(processor, store, workflow.id)
            _restore_stdout_after_tui()
            result = store.retrieve(workflow.id)
        else:
            result = wait_for_completion(store, workflow.id, _TIMEOUT_DEVELOP)
    finally:
        signal.signal(signal.SIGINT, original_sigint)
        processor.request_stop()
        processor.stop(wait=False)

    finalize_status(result, store, prefix="Resumed pipeline")
    _print_final_summary(result, changed_files=changed_files)
