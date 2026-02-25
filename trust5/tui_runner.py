"""TUI and headless runner helpers for Trust5 pipeline.

Provides functions to run workflows with the Textual-based TUI, fall back
to headless mode, and print final summaries after TUI exit.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Callable

from stabilize import Orchestrator, QueueProcessor, SqliteWorkflowStore, Workflow
from stabilize.models.status import WorkflowStatus

from .core.event_bus import init_bus
from .core.message import M, emit
from .core.runner import finalize_status, run_workflow
from .infrastructure import _cancel_stale_workflows

logger = logging.getLogger(__name__)


# Grace period before force-killing the process.  Python's atexit handler
# for concurrent.futures.ThreadPoolExecutor joins worker threads, which can
# block indefinitely if a Stabilize queue-poll loop is still running.
_FORCE_EXIT_TIMEOUT = 3.0


def _schedule_force_exit() -> None:
    """Start a daemon thread that force-exits after a grace period.

    If the process exits cleanly before the timeout, the daemon thread is
    killed automatically.  If atexit handlers hang (ThreadPoolExecutor join),
    the watchdog calls os._exit(0) to prevent a stuck terminal.
    """

    def _watchdog() -> None:
        threading.Event().wait(timeout=_FORCE_EXIT_TIMEOUT)
        os._exit(0)

    threading.Thread(target=_watchdog, daemon=True).start()


def _suppress_print_fallback() -> None:
    """Disable emit() print fallback just before TUI takes over the terminal."""
    from .core.message import set_print_fallback

    set_print_fallback(False)


def _restore_stdout_after_tui() -> None:
    """Re-enable print fallback after TUI exits.

    The TUI disables print fallback to prevent stdout corruption.
    After app.run() returns, Textual has restored the terminal and
    we need print() working again for the final summary.
    """
    from .core.message import set_print_fallback

    set_print_fallback(True)


def _print_final_summary(result: Workflow, changed_files: set[str] | None = None) -> None:
    """Print a concise final status to stdout after the TUI exits.

    Textual uses the alternate screen buffer. When it exits, all TUI
    content vanishes. This prints a visible summary so the user knows
    what happened.
    """
    status_name = result.status.name if hasattr(result.status, "name") else str(result.status)

    # Collect stage info
    stage_lines: list[str] = []
    for stage in result.stages:
        s_name = stage.ref_id
        s_status = stage.status.name if hasattr(stage.status, "name") else str(stage.status)
        if s_status in ("SUCCEEDED", "COMPLETED"):
            marker = "  OK"
        elif s_status in ("TERMINAL", "FAILED_CONTINUE", "CANCELED"):
            marker = "FAIL"
        elif s_status == "SKIPPED":
            marker = "SKIP"
        else:
            marker = " -- "
        stage_lines.append(f"  [{marker}] {s_name}")

    # Determine overall color (ANSI escape codes)
    if status_name in ("SUCCEEDED", "COMPLETED"):
        color, icon = "\033[32m", "OK"
    elif status_name == "TERMINAL":
        color, icon = "\033[31m", "FAILED"
    else:
        color, icon = "\033[33m", status_name
    reset = "\033[0m"

    print()  # TUI runner output
    print(f"{color}{'=' * 60}{reset}")  # TUI runner output
    print(f"{color}  Trust5 Pipeline: {icon}{reset}")  # TUI runner output
    print(f"{color}{'=' * 60}{reset}")  # TUI runner output
    for line in stage_lines:
        print(line)  # TUI runner output

    if changed_files:
        cwd = os.path.abspath(os.getcwd())
        print()  # TUI runner output
        print(f"  Files changed ({len(changed_files)}):")  # TUI runner output
        for fpath in sorted(changed_files):
            rel = os.path.relpath(fpath, cwd) if fpath.startswith("/") else fpath
            print(f"    {rel}")  # TUI runner output

    print()  # TUI runner output
    sys.stdout.flush()
    sys.stderr.flush()

    _schedule_force_exit()


def _wait_with_tui(
    processor: QueueProcessor,
    store: SqliteWorkflowStore,
    workflow_id: str,
) -> set[str]:
    """Launch TUI and wait for workflow completion. Returns set of changed file paths."""
    from .core.event_bus import get_bus
    from .tui.app import Trust5App

    bus = get_bus()
    if not bus:
        project_root = os.path.abspath(os.getcwd())
        bus = init_bus(project_root)

    eq = bus.subscribe()
    tui_app = Trust5App(eq, store=store, workflow_id=workflow_id)

    try:
        _suppress_print_fallback()
        tui_app.run()
    except Exception as e:  # Intentional broad catch: TUI crash fallback to headless
        # Capture changed files before entering fallback (Fix 7: preserve on crash)
        changed: set[str] = getattr(tui_app, "_changed_files", set())

        # Re-enable stdout since TUI no longer owns the terminal
        from .core.message import set_print_fallback

        set_print_fallback(True)
        emit(M.SERR, f"TUI crashed: {e}. Switching to headless.")
        # Fallback to headless wait (simple sleep loop)
        import time

        terminal = {
            WorkflowStatus.SUCCEEDED,
            WorkflowStatus.FAILED_CONTINUE,
            WorkflowStatus.TERMINAL,
            WorkflowStatus.CANCELED,
        }
        try:
            while True:
                wf = store.retrieve(workflow_id)
                if wf.status in terminal:
                    break
                time.sleep(0.5)
        except KeyboardInterrupt:
            os._exit(130)

        return changed
    finally:
        # Clean up listener to prevent memory leak in event bus
        bus.unsubscribe(eq)

    return getattr(tui_app, "_changed_files", set())


def _run_tui_mode(
    processor: QueueProcessor,
    orchestrator: Orchestrator,
    store: SqliteWorkflowStore,
    workflow: Workflow,
    timeout: float,
    label: str,
    db_path: str = "",
) -> Workflow:
    """Run workflow with TUI sidecar."""
    # 1. Start pipeline components
    store.store(workflow)
    orchestrator.start(workflow)
    emit(M.WSTR, f"{label} started: {workflow.id}")
    emit(M.SPRG, f"current=0 total={len(workflow.stages)}")

    # 2. Start processing background threads
    processor.start()

    # 3. Wait with TUI
    changed_files: set[str] = set()
    try:
        changed_files = _wait_with_tui(processor, store, workflow.id)
    finally:
        processor.request_stop()
        processor.stop(wait=False)

    # TUI exited — restore stdout for final summary
    _restore_stdout_after_tui()

    # 4. Result
    result = store.retrieve(workflow.id)
    finalize_status(result, store, prefix="Status")
    _print_final_summary(result, changed_files=changed_files)
    return result


def _run_tui_multi(run_fn: Callable[[threading.Event], Workflow | None]) -> Workflow | None:
    """Run run_fn in a background thread with a single TUI alive throughout.

    This avoids the screen-clearing problem where develop() previously created
    two separate TUI instances (plan + implement). The TUI stays in the
    alternate screen buffer for the entire duration.

    The run_fn receives a ``threading.Event`` that is set when the TUI exits
    (Ctrl+C / q). run_fn should pass this event to ``wait_for_completion()``
    so the poll loop can exit promptly and clean up its QueueProcessor.
    """
    from .core.event_bus import get_bus
    from .tui.app import Trust5App

    bus = get_bus()
    if not bus:
        bus = init_bus(os.path.abspath(os.getcwd()))

    eq = bus.subscribe()
    tui_app = Trust5App(eq)  # No store/workflow_id — won't auto-exit

    result_holder: list[Workflow | None] = [None]
    stop_event = threading.Event()

    def _background() -> None:
        try:
            result_holder[0] = run_fn(stop_event)
        except Exception as e:  # Intentional broad catch: background pipeline runner
            if not stop_event.is_set():
                emit(M.SERR, f"Pipeline failed: {e}")

    t = threading.Thread(target=_background, daemon=True)
    t.start()

    try:
        _suppress_print_fallback()
        tui_app.run()
    except Exception:  # Intentional broad catch: TUI top-level runner
        logger.debug("TUI app exited with error", exc_info=True)
    finally:
        bus.unsubscribe(eq)

    # TUI exited (user pressed q/Ctrl+C) — restore stdout for final summary
    _restore_stdout_after_tui()
    # Signal background to stop and wait for graceful shutdown.
    # The stop_event causes wait_for_completion() to return immediately,
    # allowing _pipeline()'s finally blocks to clean up QueueProcessors.
    stop_event.set()
    t.join(timeout=5.0)
    pipeline_result = result_holder[0]
    if pipeline_result is not None:
        tui_changed: set[str] = getattr(tui_app, "_changed_files", set())
        _print_final_summary(pipeline_result, changed_files=tui_changed)
    else:
        # Pipeline didn't complete — mark RUNNING workflows as CANCELED
        # so 'trust5 resume' can find and restart them.
        _cancel_stale_workflows()
        print("\nPipeline interrupted. Run 'trust5 resume' to continue.")  # TUI runner output
    # Python's atexit handler joins those threads and can block indefinitely
    # if a worker is stuck on SQLite queue polling.  Force-exit after a short
    # grace period so the user never sees a hung terminal.
    _schedule_force_exit()
    return pipeline_result


def _run_workflow_dispatch(
    processor: QueueProcessor,
    orchestrator: Orchestrator,
    store: SqliteWorkflowStore,
    workflow: Workflow,
    timeout: float,
    label: str,
    db_path: str,
    use_tui: bool = True,
) -> Workflow:
    """Dispatch to TUI or Headless runner based on configuration."""
    if use_tui:
        return _run_tui_mode(processor, orchestrator, store, workflow, timeout, label, db_path)
    return run_workflow(processor, orchestrator, store, workflow, timeout, label, db_path)
