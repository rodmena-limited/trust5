import logging
import os
import re
import shlex
import subprocess
import time
from typing import Any
from stabilize import StageExecution, Task, TaskResult
from ..core.constants import MAX_REIMPLEMENTATIONS as _MAX_REIMPL_DEFAULT
from ..core.constants import MAX_REPAIR_ATTEMPTS as _MAX_REPAIR_DEFAULT
from ..core.constants import (
    TEST_OUTPUT_LIMIT,
)
from ..core.context_keys import check_jump_limit, increment_jump_count, propagate_context
from ..core.lang import detect_language, get_profile
from ..core.message import M, emit, emit_block
from ..core.tools import _matches_test_pattern
logger = logging.getLogger(__name__)
MAX_REPAIR_ATTEMPTS = _MAX_REPAIR_DEFAULT
MAX_REIMPLEMENTATIONS = _MAX_REIMPL_DEFAULT
_FALLBACK_EXTENSIONS = (".py", ".go", ".ts", ".js", ".rs", ".java", ".rb")
_FALLBACK_SKIP_DIRS = (
    ".moai",
    ".trust5",
    ".git",
    "node_modules",
    "vendor",
    "__pycache__",
    ".venv",
    "venv",
    "target",
    "dist",
    "build",
)
_SHELL_METACHAR_RE = re.compile(r"[&|;><`$]")
_LINT_FILE_LINE_RE = re.compile(r"^(\S+?):\d+")
_FILE_NOT_FOUND_RE = re.compile(
    r"""(?:FileNotFoundError|No\s+such\s+file|can't\s+open\s+file|Cannot\s+find\s+module)"""
    r""".*?['"]([^'"]+?)['"]""",
    re.IGNORECASE,
)
_ENV_PREFIX_RE = re.compile(r"^[A-Za-z_]\w*=\S+\s")
_SOURCE_EXTENSIONS = frozenset((
    ".py", ".go", ".ts", ".js", ".tsx", ".jsx",
    ".rs", ".java", ".rb", ".c", ".cpp", ".h", ".hpp",
    ".cs", ".swift", ".kt", ".scala", ".lua", ".zig",
))
_PYTEST_RE = re.compile(r"(\d+)\s+passed")
_PYTEST_FAIL_RE = re.compile(r"(\d+)\s+failed")
_GO_RE = re.compile(r"ok\s+\S+\s+[\d.]+s")
_JEST_RE = re.compile(r"Tests:\s+.*?(\d+)\s+passed")
_GENERIC_RE = re.compile(r"(\d+)\s+tests?\s+passed", re.IGNORECASE)

def _parse_command(cmd_str: str) -> tuple[str, ...]:
    """Parse a command string into a subprocess-safe tuple.

    If the command contains shell metacharacters (&&, |, ;, etc.), starts
    with '.' (bash source), or begins with a ``VAR=value`` environment
    variable prefix, it's wrapped in ``sh -c`` to be run through a shell.
    Otherwise it's split with shlex for proper quoting.
    """
    if (
        _SHELL_METACHAR_RE.search(cmd_str)
        or cmd_str.lstrip().startswith(". ")
        or _ENV_PREFIX_RE.match(cmd_str.lstrip())
    ):
        return ("sh", "-c", cmd_str)
    try:
        return tuple(shlex.split(cmd_str))
    except ValueError:
        return tuple(cmd_str.split())
