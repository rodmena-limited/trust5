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

def generate_mutants(
    source_files: list[str],
    max_mutants: int = DEFAULT_MAX_MUTANTS,
) -> list[Mutant]:
    """Generate candidate mutations from source files.

    Scans source lines for mutation operator matches and returns a random
    sample of up to *max_mutants* candidates.
    """
    candidates: list[Mutant] = []
    for fpath in source_files:
        try:
            with open(fpath, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line_no, line in enumerate(lines, 1):
            stripped = line.lstrip()
            # Skip comments and strings-only lines (rough heuristic)
            if stripped.startswith(("#", "//", "/*", "*", "///", "---")):
                continue
            for pat, replacement, desc in _MUTATION_OPERATORS:
                if pat.search(line):
                    mutated = pat.sub(replacement, line, count=1)
                    if mutated != line:
                        candidates.append(
                            Mutant(
                                file=fpath,
                                line_no=line_no,
                                original_line=line,
                                mutated_line=mutated,
                                description=f"{os.path.basename(fpath)}:{line_no} ({desc})",
                            )
                        )
    if len(candidates) <= max_mutants:
        return candidates
    return random.sample(candidates, max_mutants)

def _apply_mutant(mutant: Mutant) -> str:
    """Apply a mutation and return the original file content for restoration."""
    with open(mutant.file, encoding="utf-8") as f:
        original_content = f.read()
    lines = original_content.splitlines(keepends=True)
    lines[mutant.line_no - 1] = mutant.mutated_line
    with open(mutant.file, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return original_content

def _restore_file(filepath: str, content: str) -> None:
    """Restore a file to its original content."""
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

@dataclass
class Mutant:
    """A single mutation to apply to a source file."""
    file: str
    line_no: int
    original_line: str
    mutated_line: str
    description: str
