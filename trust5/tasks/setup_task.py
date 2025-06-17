from __future__ import annotations
import logging
import os
import subprocess
from stabilize import StageExecution, Task, TaskResult
from ..core.message import M, emit
logger = logging.getLogger(__name__)
SETUP_TIMEOUT = 120

def _run_setup_command(cmd: str, cwd: str) -> tuple[int, str]:
    """Run a single shell command, returning (exit_code, combined_output)."""
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=SETUP_TIMEOUT,
        )
        return proc.returncode, (proc.stdout + "\n" + proc.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"command timed out after {SETUP_TIMEOUT}s"
    except Exception as e:
        return 1, str(e)

class SetupTask(Task):
    """Runs planner-specified setup commands to bootstrap the project environment."""
