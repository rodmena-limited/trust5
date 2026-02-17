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

def _filter_test_file_lint(raw_output: str, owned_files: list[str] | None = None) -> str:
    """Remove lint-error lines that the repairer cannot fix.

    Filters three categories:
    1. Lines referencing **test files** — the repairer has these in ``denied_files``.
    2. When *owned_files* is provided (parallel pipeline), lines referencing
       files NOT in the owned set — the repairer can only modify its own module's files.
    3. ``FileNotFoundError`` / "No such file" lines for missing files — safety net
       for when ``_strip_nonexistent_files`` or ``_scope_lint_command`` couldn't
       fully clean the command.  Works in both serial and parallel pipelines.

    Returns the filtered output (may be empty if all fixable errors were removed).
    """
    # Normalize owned_files paths for comparison (strip leading ./ etc.)
    owned_set: set[str] | None = None
    if owned_files:
        owned_set = set()
        for f in owned_files:
            owned_set.add(f)
            if f.startswith("./"):
                owned_set.add(f[2:])
            else:
                owned_set.add(f"./{f}")

    kept: list[str] = []
    dropped = 0
    for line in raw_output.splitlines():
        m = _LINT_FILE_LINE_RE.match(line)
        if m:
            path = m.group(1)
            # Drop test-file errors
            if _matches_test_pattern(path):
                dropped += 1
                continue
            # Drop errors in files not owned by this module
            if owned_set is not None:
                norm_path = path.lstrip("./")
                dotslash = f"./{norm_path}"
                if norm_path not in owned_set and dotslash not in owned_set and path not in owned_set:
                    dropped += 1
                    continue
        else:
            # Safety net: catch FileNotFoundError / "No such file" lines.
            # In parallel mode, filter if the missing file is not in owned_set.
            # In serial mode (no owned_set), always filter — the repair agent
            # cannot create files that the lint command expects to exist.
            fnf = _FILE_NOT_FOUND_RE.search(line)
            if fnf:
                missing = fnf.group(1)
                if owned_set is None:
                    # Serial pipeline: always drop FileNotFoundError lines
                    dropped += 1
                    continue
                # Parallel pipeline: drop only if the file is not owned
                missing_base = os.path.basename(missing)
                norm_missing = missing.lstrip("./")
                dotslash_missing = f"./{norm_missing}"
                if (
                    norm_missing not in owned_set
                    and dotslash_missing not in owned_set
                    and missing not in owned_set
                    and missing_base not in {os.path.basename(f) for f in owned_set}
                ):
                    dropped += 1
                    continue
        kept.append(line)

    # Nothing was filtered — return as-is to preserve non-standard lint output.
    if dropped == 0:
        return raw_output

    logger.debug("Filtered %d lint errors (test files or unowned)", dropped)

    result = "\n".join(kept).strip()

    # If no file-level errors remain (only summary lines like "Found 3 errors"),
    # treat as clean — all fixable errors were removed.
    if not _LINT_FILE_LINE_RE.search(result):
        return ""
    return result

def _scope_lint_command(cmd: str, owned_files: list[str]) -> str:
    """Rewrite a lint command to only reference the current module's owned files.

    In parallel pipelines, the planner generates a global lint command that lists
    ALL project files.  When a module validates before other modules have been
    implemented, the missing files cause ``FileNotFoundError`` / compile failures.

    Strategy:
    - Split on ``&&`` to preserve shell prefixes (``source venv/bin/activate``).
    - Within each segment, identify tokens ending with a known source-file extension.
    - Drop tokens whose ``os.path.basename`` is NOT in the owned set.
    - If all file tokens were removed, substitute owned file basenames as a fallback
      so the lint tool still has something to check.
    - Directory-style commands (``ruff check .``) pass through unchanged because
      they contain no file-extension tokens.
    """
    if not owned_files:
        return cmd

    owned_basenames = {os.path.basename(f) for f in owned_files}

    segments = cmd.split("&&")
    result_segments: list[str] = []

    for segment in segments:
        tokens = segment.split()
        if not tokens:
            result_segments.append(segment)
            continue

        # Detect if this segment has any source-file tokens
        file_indices: list[int] = []
        for i, token in enumerate(tokens):
            # Strip quotes that may wrap filenames
            clean = token.strip("'\"")
            _, ext = os.path.splitext(clean)
            if ext.lower() in _SOURCE_EXTENSIONS:
                file_indices.append(i)

        if not file_indices:
            # No file tokens (e.g. "source venv/bin/activate" or "ruff check .")
            result_segments.append(segment)
            continue

        # Filter: keep only tokens whose basename is in owned_files
        kept_tokens: list[str] = []
        removed_count = 0
        for i, token in enumerate(tokens):
            if i not in file_indices:
                kept_tokens.append(token)
            else:
                clean = token.strip("'\"")
                basename = os.path.basename(clean)
                if basename in owned_basenames:
                    kept_tokens.append(token)
                else:
                    removed_count += 1

        # If all file tokens were removed, substitute owned basenames as fallback
        if removed_count > 0 and removed_count == len(file_indices):
            # Re-add the non-file tokens and append owned basenames
            non_file_tokens = [t for i, t in enumerate(tokens) if i not in file_indices]
            non_file_tokens.extend(sorted(owned_basenames))
            kept_tokens = non_file_tokens

        # Preserve leading/trailing whitespace from the original segment
        leading = " " if segment.startswith(" ") else ""
        trailing = " " if segment.endswith(" ") else ""
        result_segments.append(f"{leading}{' '.join(kept_tokens)}{trailing}")

    return "&&".join(result_segments)

def _strip_nonexistent_files(cmd: str, project_root: str) -> str:
    """Remove file tokens from a lint command when the files don't exist on disk.

    The planner generates lint commands referencing files from its *plan*, but the
    implementer may create a different file structure.  Running ``py_compile`` on
    non-existent files produces ``FileNotFoundError``, which the repair agent
    cannot fix — causing an infinite validate/repair loop.

    This function checks each source-file token against the filesystem and removes
    tokens whose files don't exist.  Works for both serial and parallel pipelines.
    """
    segments = cmd.split("&&")
    result_segments: list[str] = []

    for segment in segments:
        tokens = segment.split()
        if not tokens:
            result_segments.append(segment)
            continue

        file_indices: list[int] = []
        for i, token in enumerate(tokens):
            clean = token.strip("'\"")
            _, ext = os.path.splitext(clean)
            if ext.lower() in _SOURCE_EXTENSIONS:
                file_indices.append(i)

        if not file_indices:
            result_segments.append(segment)
            continue

        kept_tokens: list[str] = []
        removed_count = 0
        for i, token in enumerate(tokens):
            if i not in file_indices:
                kept_tokens.append(token)
            else:
                clean = token.strip("'\"")
                full_path = os.path.join(project_root, clean)
                if os.path.exists(full_path):
                    kept_tokens.append(token)
                else:
                    removed_count += 1
                    logger.debug("Lint command references non-existent file: %s", clean)

        if removed_count > 0 and removed_count == len(file_indices):
            # All file tokens were non-existent.  Try to find actual source files
            # in the project and substitute them so the lint tool has something to check.
            actual_files: list[str] = []
            for dirpath, dirnames, filenames in os.walk(project_root):
                dirnames[:] = [
                    d for d in dirnames
                    if d not in _FALLBACK_SKIP_DIRS and not d.startswith(".")
                ]
                for fname in filenames:
                    _, ext = os.path.splitext(fname)
                    if ext.lower() in _SOURCE_EXTENSIONS and not _matches_test_pattern(fname):
                        rel = os.path.relpath(os.path.join(dirpath, fname), project_root)
                        actual_files.append(rel)
            if actual_files:
                non_file_tokens = [t for i, t in enumerate(tokens) if i not in file_indices]
                non_file_tokens.extend(sorted(actual_files))
                kept_tokens = non_file_tokens
                logger.info(
                    "Lint command had all non-existent files; substituted %d actual files",
                    len(actual_files),
                )
            else:
                # No source files found at all — return command as-is to avoid
                # producing an empty lint command that might behave unexpectedly.
                result_segments.append(segment)
                continue

        leading = " " if segment.startswith(" ") else ""
        trailing = " " if segment.endswith(" ") else ""
        result_segments.append(f"{leading}{' '.join(kept_tokens)}{trailing}")

    return "&&".join(result_segments)

def _build_test_env(
    project_root: str,
    profile_data: dict[str, Any],
) -> dict[str, str] | None:
    """Build subprocess environment with source roots added to the language path var.

    For projects using a non-flat layout (e.g. Python ``src/`` layout), the test
    runner can't find importable modules unless the source root is on the path.
    This reads ``source_roots`` and ``path_env_var`` from the language profile and
    returns a modified env dict — or None if no adjustment is needed.
    """
    source_roots = profile_data.get("source_roots", ())
    path_var = profile_data.get("path_env_var", "")
    if not source_roots or not path_var:
        return None

    for root in source_roots:
        src_dir = os.path.join(project_root, root)
        if os.path.isdir(src_dir):
            env = os.environ.copy()
            existing = env.get(path_var, "")
            env[path_var] = f"{src_dir}:{existing}" if existing else src_dir
            logger.debug("Added %s to %s for test subprocess", src_dir, path_var)
            return env

    return None

def _discover_test_files(
    project_root: str,
    extensions: tuple[str, ...] = _FALLBACK_EXTENSIONS,
    skip_dirs: tuple[str, ...] = _FALLBACK_SKIP_DIRS,
) -> list[str]:
    """Walk project_root and return relative paths matching test file patterns."""
    test_files: list[str] = []
    skip = set(skip_dirs)
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        for fname in filenames:
            if not any(fname.endswith(ext) for ext in extensions):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fname), project_root)
            if _matches_test_pattern(rel):
                test_files.append(rel)
    return sorted(test_files)

def _derive_module_test_files(
    all_test_files: list[str],
    owned_files: list[str],
) -> list[str]:
    """Filter discovered test files to those related to a module's owned source files.

    Uses base-name matching: for owned file ``src/engine.py``, matches test files
    whose core name (after stripping test_ prefix / _test suffix) contains ``engine``.
    Returns the filtered list, or empty if no matches found.
    """
    base_names: set[str] = set()
    for f in owned_files:
        stem = os.path.splitext(os.path.basename(f))[0].lower()
        if stem and stem != "__init__":
            base_names.add(stem)

    if not base_names:
        return []

    matched: list[str] = []
    for tf in all_test_files:
        tf_stem = os.path.splitext(os.path.basename(tf))[0].lower()
        # Strip test_ prefix and _test suffix to get the core name
        core = tf_stem
        if core.startswith("test_"):
            core = core[5:]
        if core.endswith("_test"):
            core = core[:-5]
        # Match if any owned base name appears in the core test name
        if any(bn in core for bn in base_names):
            matched.append(tf)

    return matched

def _count_tests(output: str) -> int:
    total = 0
    for line in output.splitlines():
        m = _PYTEST_RE.search(line)
        if m:
            total += int(m.group(1))
            mf = _PYTEST_FAIL_RE.search(line)
            if mf:
                total += int(mf.group(1))
            continue
        if _GO_RE.search(line):
            total += 1
            continue
        m = _JEST_RE.search(line)
        if m:
            total += int(m.group(1))
            continue
        m = _GENERIC_RE.search(line)
        if m:
            total += int(m.group(1))
    return total

class ValidateTask(Task):
    """Runs syntax checks and tests, routing failures to repair via jump_to."""
