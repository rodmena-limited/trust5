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
