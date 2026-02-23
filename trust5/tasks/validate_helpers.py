"""Helper functions for ValidateTask: command parsing, lint/test scoping, file discovery."""

import glob as _glob_module
import logging
import os
import re
import shlex
from typing import Any

from ..core.tools import _matches_test_pattern

logger = logging.getLogger(__name__)

# Minimal fallbacks for _discover_test_files signature only.
# The execute() method always resolves the real profile and passes explicit values.
_FALLBACK_EXTENSIONS = (".py", ".go", ".ts", ".js", ".rs", ".java", ".rb")
_FALLBACK_SKIP_DIRS = (
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

# Shell metacharacters that indicate a command must be run via sh -c.
_SHELL_METACHAR_RE = re.compile(r"[&|;><`$]")

# Matches a lint-output line that starts with a file path followed by :line.
# Handles BOTH concise format ("tests/test_foo.py:12:1: F401 ...")
# and ruff rich format (" --> tests/test_foo.py:12:1").
_LINT_FILE_LINE_RE = re.compile(r"^\s*(?:-->\s*)?([\S]+?):\d+", re.MULTILINE)

# Matches FileNotFoundError / "can't open file" / "No such file" messages
# that reference a missing source file.  Examples:
#   FileNotFoundError: [Errno 2] No such file or directory: 'simulations.py'
#   python: can't open file '/path/to/stats.py': [Errno 2] No such file or directory
#   Error: Cannot find module 'utils.ts'
_FILE_NOT_FOUND_RE = re.compile(
    r"""(?:FileNotFoundError|No\s+such\s+file|can't\s+open\s+file|Cannot\s+find\s+module)"""
    r""".*?['"]([^'"]+?)['"]""",
    re.IGNORECASE,
)


_ENV_PREFIX_RE = re.compile(r"^[A-Za-z_]\w*=\S+\s")


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
    1. Lines referencing **test files** -- the repairer has these in ``denied_files``.
    2. When *owned_files* is provided (parallel pipeline), lines referencing
       files NOT in the owned set -- the repairer can only modify its own module's files.
    3. ``FileNotFoundError`` / "No such file" lines for missing files -- safety net
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
            # In serial mode (no owned_set), always filter -- the repair agent
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

    # Nothing was filtered -- return as-is to preserve non-standard lint output.
    if dropped == 0:
        return raw_output

    logger.debug("Filtered %d lint errors (test files or unowned)", dropped)

    result = "\n".join(kept).strip()

    # If no file-level errors remain (only summary lines like "Found 3 errors"),
    # treat as clean -- all fixable errors were removed.
    if not _LINT_FILE_LINE_RE.search(result):
        return ""
    return result


# Source-file extensions recognized by _scope_lint_command.
_SOURCE_EXTENSIONS = frozenset(
    (
        ".py",
        ".go",
        ".ts",
        ".js",
        ".tsx",
        ".jsx",
        ".rs",
        ".java",
        ".rb",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".swift",
        ".kt",
        ".scala",
        ".lua",
        ".zig",
    )
)


def _normalize_owned_files(
    owned_files: list[str],
    project_root: str,
) -> list[str]:
    """Resolve owned_files entries that lack source extensions.

    The planner often uses module paths without extensions
    (``taskqueue/worker`` instead of ``taskqueue/worker.py``).  This checks
    the filesystem and appends the correct extension when possible.
    Works for all supported languages (Python, Go, TypeScript, etc.).
    """
    normalized: list[str] = []
    for f in owned_files:
        _, ext = os.path.splitext(f)
        if ext.lower() in _SOURCE_EXTENSIONS:
            normalized.append(f)
            continue
        full = os.path.join(project_root, f)
        # If the path is a directory, check if it has source files inside.
        # Empty directories (created by a confused test-writer) should fall
        # through to the extension-append logic below.
        if os.path.isdir(full):
            has_source = any(
                os.path.splitext(child)[1].lower() in _SOURCE_EXTENSIONS
                for child in os.listdir(full)
                if os.path.isfile(os.path.join(full, child))
            )
            if has_source:
                normalized.append(f)
                continue
            # Empty directory — fall through to try .py extension
        elif os.path.exists(full):
            # Extensionless file — keep as-is
            normalized.append(f)
            continue
        # Try appending common source extensions
        resolved = False
        for src_ext in (".py", ".go", ".ts", ".js", ".rs", ".java", ".rb"):
            if os.path.exists(full + src_ext):
                normalized.append(f + src_ext)
                resolved = True
                break
        if not resolved:
            normalized.append(f)
    if normalized != owned_files:
        logger.info("Normalized owned_files: %s -> %s", owned_files, normalized)
    return normalized


def _scope_lint_command(cmd: str, owned_files: list[str]) -> str:
    """Rewrite a lint command to only reference the current module's owned files.

    Splits on ``&&``, identifies source-file tokens, drops those not in *owned_files*.
    Directory-style commands (``ruff check .``) pass through unchanged.
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


def _strip_nonexistent_files(
    cmd: str,
    project_root: str,
    owned_files: list[str] | None = None,
) -> str:
    """Remove or glob-expand file tokens that don't exist on disk.

    Handles shell globs (e.g. ``src/*.py``) by expanding them.  In parallel
    pipelines, fallback discovery is restricted to *owned_files*.
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
        glob_expanded = False
        for i, token in enumerate(tokens):
            if i not in file_indices:
                kept_tokens.append(token)
            else:
                clean = token.strip("'\"")
                full_path = os.path.join(project_root, clean)
                if os.path.exists(full_path):
                    kept_tokens.append(token)
                else:
                    # Try glob expansion for patterns like "src/*.py"
                    expanded = sorted(_glob_module.glob(full_path))
                    if expanded:
                        glob_expanded = True
                        for ep in expanded:
                            rel = os.path.relpath(ep, project_root)
                            if not _matches_test_pattern(rel):
                                kept_tokens.append(rel)
                    else:
                        removed_count += 1
                        logger.debug("Lint command references non-existent file: %s", clean)

        if removed_count > 0 and removed_count == len(file_indices) and not glob_expanded:
            # All file tokens were non-existent.  Find replacement files.
            if owned_files:
                # Parallel pipeline: only lint the module's own files.
                actual_files = [
                    f
                    for f in owned_files
                    if os.path.exists(os.path.join(project_root, f)) and not _matches_test_pattern(f)
                ]
            else:
                # Serial pipeline: discover all source files in the project.
                actual_files = []
                for dirpath, dirnames, filenames in os.walk(project_root):
                    dirnames[:] = [d for d in dirnames if d not in _FALLBACK_SKIP_DIRS and not d.startswith(".")]
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
                # No source files found at all -- return command as-is to avoid
                # producing an empty lint command that might behave unexpectedly.
                result_segments.append(segment)
                continue

        leading = " " if segment.startswith(" ") else ""
        trailing = " " if segment.endswith(" ") else ""
        result_segments.append(f"{leading}{' '.join(kept_tokens)}{trailing}")

    return "&&".join(result_segments)


_TEST_DIR_TOKENS = frozenset({"tests/", "tests", "test/", "test", "spec/", "spec"})


def _scope_test_command(
    cmd: str,
    test_files: list[str],
) -> str:
    """Rewrite a test command to run specific files instead of a directory.

    Replaces directory tokens (``tests/``, ``test/``) with concrete file paths.
    Returns the original command unchanged if no directory tokens are found.
    """
    if not test_files:
        return cmd

    segments = cmd.split("&&")
    result_segments: list[str] = []

    for segment in segments:
        tokens = segment.split()
        if not tokens:
            result_segments.append(segment)
            continue

        dir_indices: list[int] = []
        for i, token in enumerate(tokens):
            clean = token.strip("'\"").rstrip("/")
            if clean.lower() in {t.rstrip("/") for t in _TEST_DIR_TOKENS}:
                dir_indices.append(i)

        if not dir_indices:
            result_segments.append(segment)
            continue

        # Replace directory tokens with specific test files
        new_tokens: list[str] = []
        for i, token in enumerate(tokens):
            if i in dir_indices:
                # Replace only the first directory token; drop subsequent ones
                if i == dir_indices[0]:
                    new_tokens.extend(test_files)
            else:
                new_tokens.append(token)

        leading = " " if segment.startswith(" ") else ""
        trailing = " " if segment.endswith(" ") else ""
        result_segments.append(f"{leading}{' '.join(new_tokens)}{trailing}")

    return "&&".join(result_segments)


def _build_test_env(
    project_root: str,
    profile_data: dict[str, Any],
) -> dict[str, str] | None:
    """Build subprocess environment with venv activation and source roots.

    Two adjustments are applied when relevant:

    1. **Virtualenv activation** — if ``.venv/bin`` or ``venv/bin`` exists in the
       project root, its path is prepended to ``PATH`` so that tools installed
       in the venv (ruff, pytest-timeout, etc.) are found by subprocess calls.
    2. **Source root injection** — for projects using a non-flat layout (e.g.
       Python ``src/`` layout), the source root is added to the language path var
       (``PYTHONPATH``, ``GOPATH``, …) so the test runner can find importable modules.
    """
    env: dict[str, str] | None = None

    # Activate project virtualenv if present
    for venv_dir in (".venv", "venv"):
        venv_bin = os.path.join(project_root, venv_dir, "bin")
        if os.path.isdir(venv_bin):
            env = os.environ.copy()
            env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"
            env["VIRTUAL_ENV"] = os.path.join(project_root, venv_dir)
            env.pop("PYTHONHOME", None)
            logger.debug("Activated venv at %s for subprocess", venv_bin)
            break

    # Add source roots to language path var
    source_roots = profile_data.get("source_roots", ())
    path_var = profile_data.get("path_env_var", "")
    if source_roots and path_var:
        for root in source_roots:
            src_dir = os.path.join(project_root, root)
            if os.path.isdir(src_dir):
                if env is None:
                    env = os.environ.copy()
                existing = env.get(path_var, "")
                env[path_var] = f"{src_dir}:{existing}" if existing else src_dir
                logger.debug("Added %s to %s for test subprocess", src_dir, path_var)
                break

    return env


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


_PYTEST_RE = re.compile(r"(\d+)\s+passed")
_PYTEST_FAIL_RE = re.compile(r"(\d+)\s+failed")
_GO_RE = re.compile(r"ok\s+\S+\s+[\d.]+s")
_JEST_RE = re.compile(r"Tests:\s+.*?(\d+)\s+passed")
_GENERIC_RE = re.compile(r"(\d+)\s+tests?\s+passed", re.IGNORECASE)


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


def detect_cross_module_failure(test_output: str) -> bool:
    """Return True if test output contains cross-module interface mismatch patterns.

    These patterns indicate the module's repair agent cannot fix the issue
    because the problem is in another module's code or in the interface contract
    between modules. Per-module repair wastes budget on these — the module should
    bail to integration repair where file ownership restrictions are lifted.

    Patterns detected:
    - TypeError with constructor/argument mismatches
    - AttributeError with missing attributes (wrong interface)
    - ImportError with missing names (wrong exports)
    """
    if not test_output:
        return False

    lower = test_output.lower()
    return any(
        [
            "typeerror:" in lower and ("argument" in lower or "__init__" in lower),
            "attributeerror:" in lower and "has no attribute" in lower,
            "importerror: cannot import name" in lower,
        ]
    )


# ── Exclude flags for common linters ────────────────────────────────
# Maps language -> (linter substring, exclude flag template).
# The template is appended to directory-style commands (e.g. `ruff check .`).
_LINT_EXCLUDE_FLAGS: dict[str, list[tuple[str, str]]] = {
    "python": [
        ("ruff", "--extend-exclude tests/ --extend-exclude test_* --extend-exclude *_test* --extend-exclude conftest*"),
    ],
    "typescript": [
        (
            "eslint",
            "--ignore-pattern 'test_*' --ignore-pattern '*_test*'"
            " --ignore-pattern '*.test.*' --ignore-pattern '*.spec.*'",
        ),
    ],
    "javascript": [
        (
            "eslint",
            "--ignore-pattern 'test_*' --ignore-pattern '*_test*'"
            " --ignore-pattern '*.test.*' --ignore-pattern '*.spec.*'",
        ),
    ],
    # Rust tests are inline — no exclusion needed.
    # Go gofmt/go vet don't support exclude flags — handled via file filtering.
}


def _exclude_test_files_from_lint_cmd(cmd_str: str, language: str) -> str:
    """Modify a lint command string to exclude test files BEFORE execution.

    Language-aware: uses the correct exclude flag syntax per linter.
    For linters without exclude flags (gofmt, go vet), filters explicit
    file paths using ``_matches_test_pattern()``.  Idempotent — safe to
    call on commands that already have exclude flags.

    Returns the modified command string.
    """
    if not cmd_str or not cmd_str.strip():
        return cmd_str

    # Process each &&-separated segment independently.
    segments = cmd_str.split("&&")
    result_segments: list[str] = []

    for segment in segments:
        result_segments.append(_exclude_segment(segment.strip(), language))

    return " && ".join(result_segments)


def _exclude_segment(segment: str, language: str) -> str:
    """Exclude test files from a single lint command segment."""
    if not segment:
        return segment

    tokens = segment.split()
    if not tokens:
        return segment

    # Check if this segment has explicit file-path tokens (not directory `.`).
    file_indices: list[int] = []
    has_directory_target = False
    for i, token in enumerate(tokens):
        clean = token.strip("'\"")
        _, ext = os.path.splitext(clean)
        if ext.lower() in _SOURCE_EXTENSIONS:
            file_indices.append(i)
        elif clean in (".", "./") or clean.endswith("/..."):
            has_directory_target = True

    if file_indices:
        # File-list command: filter out test file paths.
        kept: list[str] = []
        for i, token in enumerate(tokens):
            if i in file_indices:
                clean = token.strip("'\"")
                if _matches_test_pattern(clean):
                    continue
            kept.append(token)
        # If all file tokens were removed, return unchanged to avoid empty command.
        kept_file_count = sum(1 for t in kept if os.path.splitext(t.strip("'\"'"))[1].lower() in _SOURCE_EXTENSIONS)
        if kept_file_count == 0 and file_indices:
            return segment
        return " ".join(kept) if kept else segment

    if has_directory_target:
        # Directory-style command: inject language-specific exclude flags.
        lang_flags = _LINT_EXCLUDE_FLAGS.get(language, [])
        for linter_name, exclude_flag in lang_flags:
            if linter_name in segment:
                # Idempotent: don't add if already present.
                if ("--exclude" in segment or "--extend-exclude" in segment) and linter_name == "ruff":
                    return segment
                if "--ignore-pattern" in segment and linter_name == "eslint":
                    return segment
                # Insert exclude flags before the trailing directory token.
                parts = segment.split()
                insert_idx = len(parts)
                for j in range(len(parts) - 1, -1, -1):
                    clean = parts[j].strip("'\"")
                    if clean in (".", "./") or clean.endswith("/..."):
                        insert_idx = j
                        break
                flag_tokens = exclude_flag.split()
                new_parts = parts[:insert_idx] + flag_tokens + parts[insert_idx:]
                return " ".join(new_parts)

    return segment
