import json
import logging
import os
import socket
import sys
import threading
import time
from datetime import timedelta

from resilient_circuit import ExponentialDelay

# ── SQLite safety: disable mmap to prevent fork+mmap corruption on macOS ──
# subprocess.run() forks the process; mmap'd SQLite pages in the child can
# cause "attempt to write a readonly database" when the child exits.
os.environ.setdefault("STABILIZE_SQLITE_MMAP_SIZE_MB", "0")
# Use FULL synchronous mode for maximum safety (trust5 is I/O-bound on
# LLM calls, so the extra fsync cost is negligible).
os.environ.setdefault("STABILIZE_SQLITE_SYNCHRONOUS", "FULL")
# ── Stabilize watchdog: raise the CompleteWorkflow poll limit ──────────
# Default is 240 retries × 15 s = 1 hour.  Trust5 pipelines can run for
# days/weeks for large projects.  57600 × 15 s = 10 days.
os.environ.setdefault("STABILIZE_MAX_STAGE_WAIT_RETRIES", "57600")

import typer
from stabilize import Workflow

from .commands.resume_cmd import resume_logic
from .core import constants
from .core.config import ensure_global_config
from .core.git import GitManager
from .core.init import ProjectInitializer
from .core.message import M, emit
from .core.plan_parser import parse_plan_output
from .core.runner import finalize_status, wait_for_completion
from .core.tools import Tools
from .infrastructure import (
    _init_viewer_once,
    _setup_phase,
    setup_stabilize,
)
from .tui_runner import (
    _run_tui_multi,
    _run_workflow_dispatch,
)
from .workflows.loop_workflow import create_loop_workflow
from .workflows.parallel_pipeline import (
    create_parallel_develop_workflow,
    extract_plan_output,
    parse_modules,
)
from .workflows.pipeline import create_develop_workflow, create_plan_only_workflow, strip_plan_stage
from .workflows.plan import create_plan_workflow
from .workflows.run import create_run_workflow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

app = typer.Typer()

_USE_TUI = True


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


@app.callback()
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
    ensure_global_config()


# ── CLI Commands ─────────────────────────────────────────────────────────


@app.command()
def plan(request: str) -> None:
    Tools.set_non_interactive(True)
    processor, orchestrator, store, _queue, db_path = setup_stabilize(use_tui=_USE_TUI)
    workflow = create_plan_workflow(request)
    _run_workflow_dispatch(
        processor,
        orchestrator,
        store,
        workflow,
        constants.TIMEOUT_PLAN,
        "Plan",
        db_path,
        use_tui=_USE_TUI,
    )


@app.command()
def develop(request: str) -> None:
    Tools.set_non_interactive(True)
    _init_viewer_once(_USE_TUI)

    def _pipeline(shutdown: threading.Event | None = None) -> Workflow | None:
        """Run plan -> implement pipeline.

        *shutdown* is a ``threading.Event`` set by ``_run_tui_multi`` when the
        TUI exits (Ctrl+C).  It is threaded into ``wait_for_completion`` so the
        poll loop can exit promptly, and checked between phases so processors
        get cleaned up before the thread terminates.
        """
        # Phase 1: Plan (with retry on empty output)
        max_plan_attempts = 3
        _plan_backoff = ExponentialDelay(
            min_delay=timedelta(seconds=10),
            max_delay=timedelta(seconds=60),
            factor=2,
            jitter=0.3,
        )
        plan_output = ""
        plan_result = None

        for plan_attempt in range(1, max_plan_attempts + 1):
            processor, orchestrator, store, _queue, db_path = setup_stabilize(use_tui=_USE_TUI)
            plan_wf = create_plan_only_workflow(request)

            store.store(plan_wf)
            orchestrator.start(plan_wf)
            emit(M.WSTR, f"Plan started (attempt {plan_attempt}/{max_plan_attempts}): {plan_wf.id}")
            processor.start()

            try:
                plan_result = wait_for_completion(
                    store,
                    plan_wf.id,
                    constants.TIMEOUT_PLAN,
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

            if plan_attempt < max_plan_attempts:
                emit(
                    M.SWRN,
                    f"Plan phase produced no output (attempt {plan_attempt}/{max_plan_attempts}) — retrying in 10s",
                )
                time.sleep(_plan_backoff.for_attempt(plan_attempt))
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
                if stage.ref_id in ("write_tests", "implement", "validate", "quality", "review"):
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
                constants.TIMEOUT_DEVELOP,
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


@app.command()
def run(spec_id: str) -> None:
    Tools.set_non_interactive(True)
    processor, orchestrator, store, _queue, db_path = setup_stabilize(use_tui=_USE_TUI)
    workflow = create_run_workflow(spec_id)
    _run_workflow_dispatch(
        processor,
        orchestrator,
        store,
        workflow,
        constants.TIMEOUT_RUN,
        "Run",
        db_path,
        use_tui=_USE_TUI,
    )


@app.command()
def init(path: str = ".") -> None:
    ensure_global_config()
    initializer = ProjectInitializer(path)
    initializer.run_wizard()
    GitManager(path).init_repo()


@app.command()
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
    except (OSError, ValueError, RuntimeError) as e:  # login: network/auth/config errors
        emit(M.SERR, f"Login failed: {e}")
        raise typer.Exit(1)


@app.command()
def logout(provider: str | None = None) -> None:
    from .core.auth.registry import do_logout

    if do_logout(provider):
        emit(M.WSUC, f"Logged out from {provider or 'active provider'}.")
    else:
        emit(M.SWRN, "No active session to log out from.")


@app.command(name="auth-status")
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


@app.command()
def loop() -> None:
    Tools.set_non_interactive(True)
    processor, orchestrator, store, _queue, db_path = setup_stabilize(use_tui=_USE_TUI)
    workflow = create_loop_workflow()
    _run_workflow_dispatch(
        processor, orchestrator, store, workflow, constants.TIMEOUT_LOOP, "Ralph Loop", db_path, use_tui=_USE_TUI
    )


@app.command()
def resume() -> None:
    """Resume the last TERMINAL pipeline from its failed stage."""
    resume_logic(use_tui=_USE_TUI)


@app.command()
def watch(path: str = ".") -> None:
    """Stream events from a running trust5 pipeline to stdout."""
    sock_path = os.path.join(os.path.abspath(path), ".trust5", "events.sock")
    if not os.path.exists(sock_path):
        emit(M.SERR, f"No active pipeline found (socket not found: {sock_path})")
        raise typer.Exit(1)

    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.connect(sock_path)
    except (ConnectionRefusedError, OSError) as exc:
        emit(M.SERR, f"Cannot connect to pipeline: {exc}")
        raise typer.Exit(1)

    emit(M.SINF, f"Connected to pipeline at {sock_path}. Press Ctrl+C to detach.")
    buf = ""
    try:
        while True:
            data = conn.recv(4096)
            if not data:
                emit(M.SINF, "Pipeline disconnected.")
                break
            buf += data.decode("utf-8", errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                if not line.strip():
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                _render_watch_event(evt)
    except KeyboardInterrupt:
        emit(M.SINF, "Detached from pipeline.")
    finally:
        conn.close()


def _render_watch_event(evt: dict[str, str]) -> None:
    kind = evt.get("k", "")
    code = evt.get("c", "")
    ts = evt.get("t", "")
    msg = evt.get("m", "")
    label = evt.get("l", "")
    tag = f"{{{code}}}"

    # CLI user output
    if kind == "msg":
        print(f"{tag}{ts} {msg}", flush=True)  # CLI user output
    elif kind == "bs":
        print(f"{tag}{ts} \u250c\u2500\u2500 {label}", flush=True)  # CLI user output
    elif kind == "bl":
        print(f"{tag}{ts}  \u2502 {msg}", flush=True)  # CLI user output
    elif kind == "be":
        print(f"{tag}{ts} \u2514\u2500\u2500", flush=True)  # CLI user output
    elif kind == "ss":
        print(f"{tag}{ts} {label}", end="", flush=True)  # CLI user output
    elif kind == "st":
        sys.stdout.write(msg)
        sys.stdout.flush()
    elif kind == "se":
        print("", flush=True)  # CLI user output


if __name__ == "__main__":
    app()
