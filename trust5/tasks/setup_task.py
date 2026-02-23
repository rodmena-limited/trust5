"""Setup task — runs planner-specified environment setup commands.

Executes commands like ``python3 -m venv .venv`` and ``pip install -r
requirements.txt`` that the planner determined are needed for the project.
This replaces the old "NEVER run pip install" prohibition with LLM-driven
environment bootstrapping.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess

from stabilize import StageExecution, Task, TaskResult

from ..core.constants import SETUP_TIMEOUT
from ..core.message import M, emit

logger = logging.getLogger(__name__)


class SetupTask(Task):
    """Runs planner-specified setup commands to bootstrap the project environment."""

    def execute(self, stage: StageExecution) -> TaskResult:
        project_root = stage.context.get("project_root", os.getcwd())
        setup_commands: list[str] = stage.context.get("setup_commands", [])

        if not setup_commands:
            emit(M.SINF, "No setup commands specified by planner — skipping setup.")
            return TaskResult.success(outputs={"setup_completed": True, "setup_skipped": True})

        emit(M.SINF, f"Running {len(setup_commands)} setup command(s) in {project_root}")

        failed: list[str] = []
        for i, cmd in enumerate(setup_commands, 1):
            emit(M.SINF, f"  [{i}/{len(setup_commands)}] {cmd}")
            rc, out = _run_setup_command(cmd, project_root)
            if rc != 0:
                emit(M.SWRN, f"  Setup command failed (rc={rc}): {cmd}\n{out[:500]}")
                failed.append(cmd)
            else:
                emit(M.SINF, f"  OK ({len(out)} chars output)")

        if failed:
            emit(
                M.SWRN,
                f"Setup completed with {len(failed)} failure(s). Agent may need to fix environment issues.",
            )
            return TaskResult.failed_continue(
                error=f"Setup failed for {len(failed)} command(s): {failed}",
                outputs={
                    "setup_completed": False,
                    "setup_failed_commands": failed,
                },
            )

        emit(M.SINF, "All setup commands completed successfully.")
        return TaskResult.success(
            outputs={
                "setup_completed": True,
                "setup_failed_commands": [],
            }
        )


def _quote_version_specifiers(cmd: str) -> str:
    """Quote pip/uv version specifiers so the shell does not interpret >= as redirection.

    Turns ``pip install flask>=3.0.0 pytest>=8.0`` into
    ``pip install 'flask>=3.0.0' 'pytest>=8.0'`` while leaving other commands untouched.
    """
    # Only process pip/uv install commands
    if not re.search(r"\bpip\b.*\binstall\b|\buv\b.*\binstall\b", cmd):
        return cmd

    # Quote whitespace-delimited tokens that contain version specifiers
    # (>=, <=, ==, !=, ~=, <, >) but skip tokens already inside quotes.
    def _quote_token(m: re.Match[str]) -> str:
        token = m.group(0)
        start = m.start()
        if start > 0 and cmd[start - 1] in ("'", '"'):
            return token
        return f"'{token}'"

    return re.sub(
        r"(?<=\s)([a-zA-Z0-9_][a-zA-Z0-9_.\-\[\]]*(?:>=|<=|==|!=|~=|<|>)[^\s'\"]*)",
        _quote_token,
        cmd,
    )


def _run_setup_command(cmd: str, cwd: str) -> tuple[int, str]:
    """Run a single shell command, returning (exit_code, combined_output).

    Version specifiers in pip/uv install commands are auto-quoted to prevent
    the shell from interpreting ``>=`` as stdout redirection.
    """
    safe_cmd = _quote_version_specifiers(cmd)
    try:
        proc = subprocess.run(
            safe_cmd,
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
