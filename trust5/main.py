import atexit
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable
from typing import Any
import typer
from stabilize import (
    Orchestrator,
    QueueProcessor,
    ShellTask,
    SqliteQueue,
    SqliteWorkflowStore,
    TaskRegistry,
    Workflow,
)
from stabilize.events import SqliteEventStore, configure_event_sourcing
from stabilize.models.status import WorkflowStatus
from stabilize.recovery import recover_on_startup
from .core.agent_task import AgentTask
from .core.constants import TIMEOUT_DEVELOP as _TIMEOUT_DEVELOP
from .core.constants import TIMEOUT_LOOP as _TIMEOUT_LOOP
from .core.constants import TIMEOUT_PLAN as _TIMEOUT_PLAN
from .core.constants import TIMEOUT_RUN as _TIMEOUT_RUN
from .core.event_bus import init_bus, shutdown_bus
from .core.git import GitManager
from .core.implementer_task import ImplementerTask
from .core.init import ProjectInitializer
from .core.loop import LoopTask
from .core.mcp_manager import init_mcp, shutdown_mcp
from .core.message import M, emit
from .core.plan_parser import parse_plan_output
from .core.runner import finalize_status, run_workflow, wait_for_completion
from .core.tools import Tools
from .core.viewer import StdoutViewer
from .tasks.mutation_task import MutationTask
from .tasks.quality_task import QualityTask
from .tasks.repair_task import RepairTask
from .tasks.setup_task import SetupTask
from .tasks.validate_task import ValidateTask
from .workflows.loop_workflow import create_loop_workflow
from .workflows.parallel_pipeline import (
    create_parallel_develop_workflow,
    extract_plan_output,
    parse_modules,
)
from .workflows.pipeline import create_develop_workflow, create_plan_only_workflow, strip_plan_stage
from .workflows.plan import create_plan_workflow
from .workflows.run import create_run_workflow
logger = logging.getLogger(__name__)
app = typer.Typer()
_USE_TUI = True
TIMEOUT_PLAN = _TIMEOUT_PLAN
TIMEOUT_DEVELOP = _TIMEOUT_DEVELOP
TIMEOUT_RUN = _TIMEOUT_RUN
TIMEOUT_LOOP = _TIMEOUT_LOOP
_viewer_initialized = False
_event_sourcing_configured = False

def _silence_logging_for_tui() -> None:
    """Redirect logging to a file when TUI mode is selected.

    Textual owns the terminal (stdin/stdout/stderr). Any logging output
    to stderr corrupts the TUI layout, causing raw text to bleed through.

    NOTE: We redirect logging early, but keep emit() print fallback ON.
    This allows pre-TUI messages (errors, warnings, early exits) to be
    visible on stdout. Print fallback is disabled just before app.run()
    via _suppress_print_fallback().
    """
    log_dir = os.path.join(os.path.abspath(os.getcwd()), ".trust5")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "trust5.log")

    file_handler = logging.FileHandler(log_file, mode="a")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    )

    root = logging.getLogger()
    # Remove all stderr handlers
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    root.addHandler(file_handler)

def _suppress_print_fallback() -> None:
    """Disable emit() print fallback just before TUI takes over the terminal."""
    from .core.message import set_print_fallback

    set_print_fallback(False)

def _global_options(
    provider: str = typer.Option(
        "",
        "--provider",
        "-p",
        help="Auth provider override (claude, google, ollama)",
    ),
    headless: bool = typer.Option(
        False,
        "--headless",
        help="Run without TUI (stdout only)",
    ),
) -> None:
    if provider:
        from .core.auth.registry import set_provider_override

        set_provider_override(provider)

    global _USE_TUI
    _USE_TUI = not headless

    # Auto-disable TUI if output is piped (e.g. | tee)
    if not sys.stdout.isatty():
        _USE_TUI = False

    if _USE_TUI:
        _silence_logging_for_tui()

def _resolve_db_path() -> str:
    db_dir = os.path.join(os.path.abspath(os.getcwd()), ".trust5")
    os.makedirs(db_dir, exist_ok=True)
    return os.path.join(db_dir, "trust5.db")

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

    print()
    print(f"{color}{'=' * 60}{reset}")
    print(f"{color}  Trust5 Pipeline: {icon}{reset}")
    print(f"{color}{'=' * 60}{reset}")
    for line in stage_lines:
        print(line)

    if changed_files:
        cwd = os.path.abspath(os.getcwd())
        print()
        print(f"  Files changed ({len(changed_files)}):")
        for fpath in sorted(changed_files):
            rel = os.path.relpath(fpath, cwd) if fpath.startswith("/") else fpath
            print(f"    {rel}")

    print()
    sys.stdout.flush()
    sys.stderr.flush()

    # Start a cancellable watchdog to force-kill if atexit handlers hang.
    # QueueProcessor uses concurrent.futures.ThreadPoolExecutor internally;
    # Python's _python_exit() atexit handler joins those threads and can block
    # indefinitely if a worker is stuck on SQLite queue polling.
    watchdog_event = threading.Event()

    def _watchdog() -> None:
        if not watchdog_event.wait(timeout=5.0):
            os._exit(0)

    threading.Thread(target=_watchdog, daemon=True).start()

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
    except Exception as e:
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

def _run_workflow_dispatch(
    processor: QueueProcessor,
    orchestrator: Orchestrator,
    store: SqliteWorkflowStore,
    workflow: Workflow,
    timeout: float,
    label: str,
    db_path: str,
) -> Workflow:
    """Dispatch to TUI or Headless runner based on configuration."""
    if _USE_TUI:
        return _run_tui_mode(processor, orchestrator, store, workflow, timeout, label, db_path)
    return run_workflow(processor, orchestrator, store, workflow, timeout, label, db_path)

def _build_task_registry() -> TaskRegistry:
    registry = TaskRegistry()
    registry.register("agent", AgentTask)
    registry.register("implementer", ImplementerTask)
    registry.register("loop", LoopTask)
    registry.register("mutation", MutationTask)
    registry.register("setup", SetupTask)
    registry.register("validate", ValidateTask)
    registry.register("repair", RepairTask)
    registry.register("quality", QualityTask)
    registry.register("shell", ShellTask)
    return registry

def _emit_provider_info() -> None:
    from .core.auth.registry import get_active_token

    active = get_active_token()
    if active is not None:
        provider, _token_data = active
        emit(M.MPRF, f"provider={provider.config.name} backend={provider.config.backend}")
    else:
        emit(M.MPRF, "provider=ollama backend=ollama")

def _configure_event_sourcing_once(conn_str: str) -> None:
    global _event_sourcing_configured
    if _event_sourcing_configured:
        return
    _event_sourcing_configured = True
    event_store = SqliteEventStore(conn_str, create_tables=True)
    configure_event_sourcing(event_store)

def _setup_phase() -> tuple[QueueProcessor, Orchestrator, SqliteWorkflowStore, SqliteQueue, str]:
    db_path = _resolve_db_path()
    conn_str = f"sqlite:///{db_path}"

    store = SqliteWorkflowStore(conn_str, create_tables=True)
    queue = SqliteQueue(conn_str, table_name="queue_messages")
    queue._create_table()

    processor = QueueProcessor(queue, store=store, task_registry=_build_task_registry())
    orchestrator = Orchestrator(queue)

    return processor, orchestrator, store, queue, db_path

def _shutdown_ipc(viewer: StdoutViewer) -> None:
    viewer.stop()
    shutdown_bus()

def _init_viewer_once() -> None:
    global _viewer_initialized
    if _viewer_initialized:
        return
    _viewer_initialized = True
    project_root = os.path.abspath(os.getcwd())
    bus = init_bus(project_root)

    # Only start StdoutViewer if TUI is NOT enabled (headless mode)
    if not _USE_TUI:
        viewer = StdoutViewer(bus)
        viewer.start()
        atexit.register(_shutdown_ipc, viewer)

def setup_stabilize() -> tuple[QueueProcessor, Orchestrator, SqliteWorkflowStore, SqliteQueue, str]:
    db_path = _resolve_db_path()
    conn_str = f"sqlite:///{db_path}"

    _init_viewer_once()
    init_mcp()
    atexit.register(shutdown_mcp)
    _configure_event_sourcing_once(conn_str)

    store = SqliteWorkflowStore(conn_str, create_tables=True)
    queue = SqliteQueue(conn_str, table_name="queue_messages")
    queue._create_table()

    try:
        recovered = recover_on_startup(store, queue, application="trust5")
        if recovered:
            emit(M.WRCV, f"Recovered {len(recovered)} pending workflow(s)")
    except Exception as e:
        emit(M.SWRN, f"Recovery check failed (non-fatal): {e}")

    processor = QueueProcessor(queue, store=store, task_registry=_build_task_registry())
    orchestrator = Orchestrator(queue)

    _emit_provider_info()

    return processor, orchestrator, store, queue, db_path

def plan(request: str) -> None:
    Tools.set_non_interactive(True)
    processor, orchestrator, store, _queue, db_path = setup_stabilize()
    workflow = create_plan_workflow(request)
    _run_workflow_dispatch(processor, orchestrator, store, workflow, TIMEOUT_PLAN, "Plan", db_path)

def _cancel_stale_workflows() -> None:
    """Mark any RUNNING trust5 workflows as CANCELED so 'resume' can find them."""
    try:
        from stabilize.persistence.store import WorkflowCriteria

        db_path = _resolve_db_path()
        conn_str = f"sqlite:///{db_path}"
        store = SqliteWorkflowStore(conn_str)
        criteria = WorkflowCriteria(
            statuses={WorkflowStatus.RUNNING},
            page_size=10,
        )
        for wf in store.retrieve_by_application("trust5", criteria):
            wf.status = WorkflowStatus.CANCELED
            store.update_status(wf)
    except Exception:
        pass  # best-effort — don't block exit

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
        except Exception as e:
            if not stop_event.is_set():
                emit(M.SERR, f"Pipeline failed: {e}")

    t = threading.Thread(target=_background, daemon=True)
    t.start()

    try:
        _suppress_print_fallback()
        tui_app.run()
    except Exception:
        pass
    finally:
        bus.unsubscribe(eq)

    # TUI exited (user pressed q/Ctrl+C) — restore stdout for final summary
    _restore_stdout_after_tui()

    # Signal background to stop and wait for graceful shutdown.
    # The stop_event causes wait_for_completion() to return immediately,
    # allowing _pipeline()'s finally blocks to clean up QueueProcessors.
    stop_event.set()
    t.join(timeout=5.0)

    result = result_holder[0]
    if result is not None:
        tui_changed: set[str] = getattr(tui_app, "_changed_files", set())
        _print_final_summary(result, changed_files=tui_changed)
    else:
        # Pipeline didn't complete — mark RUNNING workflows as CANCELED
        # so 'trust5 resume' can find and restart them.
        _cancel_stale_workflows()
        print("\nPipeline interrupted. Run 'trust5 resume' to continue.")

    return result

def develop(request: str) -> None:
    Tools.set_non_interactive(True)
    _init_viewer_once()

    def _pipeline(shutdown: threading.Event | None = None) -> Workflow | None:
        """Run plan → implement pipeline.

        *shutdown* is a ``threading.Event`` set by ``_run_tui_multi`` when the
        TUI exits (Ctrl+C).  It is threaded into ``wait_for_completion`` so the
        poll loop can exit promptly, and checked between phases so processors
        get cleaned up before the thread terminates.
        """
        # Phase 1: Plan (with retry on empty output)
        MAX_PLAN_ATTEMPTS = 3
        plan_output = ""
        plan_result = None

        for plan_attempt in range(1, MAX_PLAN_ATTEMPTS + 1):
            processor, orchestrator, store, _queue, db_path = setup_stabilize()
            plan_wf = create_plan_only_workflow(request)

            store.store(plan_wf)
            orchestrator.start(plan_wf)
            emit(M.WSTR, f"Plan started (attempt {plan_attempt}/{MAX_PLAN_ATTEMPTS}): {plan_wf.id}")
            processor.start()

            try:
                plan_result = wait_for_completion(
                    store,
                    plan_wf.id,
                    TIMEOUT_PLAN,
                    stop_event=shutdown,
                )
            finally:
                processor.request_stop()
                processor.stop(wait=False)

            # If the user quit during planning, exit early.
            if shutdown is not None and shutdown.is_set():
                return None

            finalize_status(plan_result, store, prefix="Plan")

            plan_output = extract_plan_output(plan_result)
            if plan_output and plan_output.strip():
                break  # Got usable output

            if plan_attempt < MAX_PLAN_ATTEMPTS:
                emit(
                    M.SWRN,
                    f"Plan phase produced no output (attempt {plan_attempt}/{MAX_PLAN_ATTEMPTS}) "
                    f"— retrying in 10s",
                )
                time.sleep(10)
            else:
                emit(M.WFAL, "Plan phase produced no output after all attempts — cannot proceed.")
                return None

        modules = parse_modules(plan_result)
        plan_config = parse_plan_output(plan_output)
        emit(
            M.SINF,
            f"Planner produced {len(modules)} module(s), "
            f"threshold={plan_config.quality_threshold}, "
            f"setup_cmds={len(plan_config.setup_commands)}",
        )

        plan_config_dict = plan_config.to_dict()

        # Phase 2: Implement (fresh processor/store to avoid stale state)
        p2_processor, p2_orchestrator, p2_store, _q2, db2 = _setup_phase()

        if len(modules) <= 1:
            serial_wf = create_develop_workflow(request)
            stripped = strip_plan_stage(serial_wf.stages, plan_output)
            for stage in stripped:
                if stage.ref_id == "setup":
                    stage.context["setup_commands"] = list(plan_config.setup_commands)
                if stage.ref_id in ("write_tests", "implement", "validate", "quality"):
                    stage.context["plan_config"] = plan_config_dict
            impl_wf = Workflow.create(
                application="trust5",
                name="Develop Pipeline",
                stages=stripped,
            )
        else:
            impl_wf = create_parallel_develop_workflow(
                modules,
                request,
                plan_output,
                setup_commands=list(plan_config.setup_commands),
                plan_config_dict=plan_config_dict,
            )

        p2_store.store(impl_wf)
        p2_orchestrator.start(impl_wf)
        emit(M.WSTR, f"Implement started: {impl_wf.id}")
        emit(M.SPRG, f"current=0 total={len(impl_wf.stages)} modules={len(modules)}")
        p2_processor.start()

        try:
            impl_result = wait_for_completion(
                p2_store,
                impl_wf.id,
                TIMEOUT_DEVELOP,
                stop_event=shutdown,
            )
        finally:
            p2_processor.request_stop()
            p2_processor.stop(wait=False)

        if shutdown is not None and shutdown.is_set():
            return None

        finalize_status(impl_result, p2_store, prefix="Status")
        return impl_result

    if _USE_TUI:
        _run_tui_multi(_pipeline)
    else:
        _pipeline()

def run(spec_id: str) -> None:
    Tools.set_non_interactive(True)
    processor, orchestrator, store, _queue, db_path = setup_stabilize()
    workflow = create_run_workflow(spec_id)
    _run_workflow_dispatch(processor, orchestrator, store, workflow, TIMEOUT_RUN, "Run", db_path)

def init(path: str = ".") -> None:
    initializer = ProjectInitializer(path)
    initializer.run_wizard()
    GitManager(path).init_repo()

def login(provider: str) -> None:
    from .core.auth.registry import do_login, list_providers

    available = list_providers()
    if provider not in available:
        emit(M.SERR, f"Unknown provider '{provider}'. Available: {', '.join(available)}")
        raise typer.Exit(1)

    try:
        token_data = do_login(provider)
        expires_min = int(token_data.expires_in_seconds / 60)
        emit(M.WSUC, f"Logged in to {provider}. Token expires in {expires_min} min.")
    except Exception as e:
        emit(M.SERR, f"Login failed: {e}")
        raise typer.Exit(1)

def logout(provider: str | None = None) -> None:
    from .core.auth.registry import do_logout

    if do_logout(provider):
        emit(M.WSUC, f"Logged out from {provider or 'active provider'}.")
    else:
        emit(M.SWRN, "No active session to log out from.")

def auth_status() -> None:
    from .core.auth.token_store import TokenStore

    store = TokenStore()
    active = store.get_active()
    providers = store.list_providers()

    if not providers:
        emit(M.SWRN, "No providers authenticated. Run 'trust5 login <provider>'.")
        return

    for name in providers:
        token = store.load(name)
        is_active = name == active
        marker = " (active)" if is_active else ""
        if token and not token.is_expired:
            mins = int(token.expires_in_seconds / 60)
            emit(M.WSUC, f"  {name}{marker}: authenticated (expires in {mins} min)")
        elif token and token.is_expired:
            emit(M.WFAL, f"  {name}{marker}: token expired (needs refresh)")
        else:
            emit(M.WFAL, f"  {name}{marker}: no token")

def loop() -> None:
    Tools.set_non_interactive(True)
    processor, orchestrator, store, _queue, db_path = setup_stabilize()
    workflow = create_loop_workflow()
    _run_workflow_dispatch(processor, orchestrator, store, workflow, TIMEOUT_LOOP, "Ralph Loop", db_path)

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
