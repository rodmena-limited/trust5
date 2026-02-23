"""Infrastructure setup for Trust5 pipeline.

Provides Stabilize workflow engine bootstrapping: task registry, DB paths,
event sourcing, viewer initialization, and stale-workflow cleanup.
"""

from __future__ import annotations

import atexit
import logging
import os
from typing import TYPE_CHECKING

from stabilize import (
    Orchestrator,
    QueueProcessor,
    ShellTask,
    SqliteQueue,
    SqliteWorkflowStore,
    TaskRegistry,
)
from stabilize.events import SqliteEventStore, configure_event_sourcing
from stabilize.models.status import WorkflowStatus
from stabilize.recovery import recover_on_startup

from .core.agent_task import AgentTask
from .core.event_bus import init_bus, shutdown_bus
from .core.implementer_task import ImplementerTask
from .core.loop import LoopTask
from .core.mcp_manager import init_mcp, shutdown_mcp
from .core.message import M, emit
from .core.viewer import StdoutViewer
from .tasks.mutation_task import MutationTask
from .tasks.quality_task import QualityTask
from .tasks.repair_task import RepairTask
from .tasks.review_task import ReviewTask
from .tasks.setup_task import SetupTask
from .tasks.validate_task import ValidateTask
from .tasks.watchdog_task import WatchdogTask

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ── Module-level guards for idempotent init ──────────────────────────────

_viewer_initialized = False
_event_sourcing_configured = False


def _resolve_db_path() -> str:
    db_dir = os.path.join(os.path.abspath(os.getcwd()), ".trust5")
    os.makedirs(db_dir, exist_ok=True)
    return os.path.join(db_dir, "trust5.db")


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
    registry.register("review", ReviewTask)
    registry.register("shell", ShellTask)
    registry.register("watchdog", WatchdogTask)
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


def _shutdown_ipc(viewer: StdoutViewer) -> None:
    viewer.stop()
    shutdown_bus()


def _init_viewer_once(use_tui: bool) -> None:
    """Initialize the event bus and (in headless mode) the StdoutViewer.

    Idempotent — safe to call multiple times.
    """
    global _viewer_initialized
    if _viewer_initialized:
        return
    _viewer_initialized = True
    project_root = os.path.abspath(os.getcwd())
    bus = init_bus(project_root)

    # Only start StdoutViewer if TUI is NOT enabled (headless mode)
    if not use_tui:
        viewer = StdoutViewer(bus)
        viewer.start()
        atexit.register(_shutdown_ipc, viewer)


def setup_stabilize(
    use_tui: bool = True,
) -> tuple[QueueProcessor, Orchestrator, SqliteWorkflowStore, SqliteQueue, str]:
    db_path = _resolve_db_path()
    conn_str = f"sqlite:///{db_path}"

    _init_viewer_once(use_tui)
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


def _setup_phase() -> tuple[QueueProcessor, Orchestrator, SqliteWorkflowStore, SqliteQueue, str]:
    db_path = _resolve_db_path()
    conn_str = f"sqlite:///{db_path}"

    store = SqliteWorkflowStore(conn_str, create_tables=True)
    queue = SqliteQueue(conn_str, table_name="queue_messages")
    queue._create_table()

    processor = QueueProcessor(queue, store=store, task_registry=_build_task_registry())
    orchestrator = Orchestrator(queue)

    return processor, orchestrator, store, queue, db_path


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
        logger.debug("Best-effort stale workflow cleanup failed", exc_info=True)
