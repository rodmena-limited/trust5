import logging
import os
import random
import re
import subprocess
from dataclasses import dataclass
from typing import Any
from stabilize import StageExecution, Task, TaskResult
from ..core.lang import LanguageProfile
from ..core.message import M, emit
logger = logging.getLogger(__name__)
SUBPROCESS_TIMEOUT = 120
DEFAULT_MAX_MUTANTS = 10
_MUTATION_OPERATORS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"(?<!=)(?<![!<>])==(?!=)"), "!=", "eq→neq"),
    (re.compile(r"(?<!=)!=(?!=)"), "==", "neq→eq"),
    (re.compile(r"(?<!=)>="), ">", "gte→gt"),
    (re.compile(r"(?<!=)<="), "<", "lte→lt"),
    (re.compile(r"(?<![<!=])>(?![>=])"), ">=", "gt→gte"),
    (re.compile(r"(?<![>!=])<(?![<=])"), "<=", "lt→lte"),
    (re.compile(r"\bTrue\b"), "False", "true→false"),
    (re.compile(r"\bFalse\b"), "True", "false→true"),
    (re.compile(r"\btrue\b"), "false", "true→false"),
    (re.compile(r"\bfalse\b"), "true", "false→true"),
]
_TEST_PATTERN = re.compile(r"(test_|_test\.|\.test\.|spec_|_spec\.)", re.IGNORECASE)

def _find_source_files(
    project_root: str,
    extensions: tuple[str, ...],
    skip_dirs: tuple[str, ...],
) -> list[str]:
    """Find non-test source files."""
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        for fname in filenames:
            if _TEST_PATTERN.search(fname):
                continue
            if any(fname.endswith(ext) for ext in extensions):
                files.append(os.path.join(dirpath, fname))
    return files

@dataclass
class Mutant:
    """A single mutation to apply to a source file."""
    file: str
    line_no: int
    original_line: str
    mutated_line: str
    description: str
