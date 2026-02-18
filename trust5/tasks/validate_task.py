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

# Minimal fallbacks for _discover_test_files signature only.
# The execute() method always resolves the real profile and passes explicit values.
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

# Shell metacharacters that indicate a command must be run via sh -c.
_SHELL_METACHAR_RE = re.compile(r"[&|;><`$]")

# Matches a lint-output line that starts with a file path followed by :line
# e.g. "tests/test_foo.py:12:1: F401 ..."
_LINT_FILE_LINE_RE = re.compile(r"^(\S+?):\d+")

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


# Source-file extensions recognized by _scope_lint_command.
_SOURCE_EXTENSIONS = frozenset((
    ".py", ".go", ".ts", ".js", ".tsx", ".jsx",
    ".rs", ".java", ".rb", ".c", ".cpp", ".h", ".hpp",
    ".cs", ".swift", ".kt", ".scala", ".lua", ".zig",
))


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


def _strip_nonexistent_files(
    cmd: str,
    project_root: str,
    owned_files: list[str] | None = None,
) -> str:
    """Remove file tokens from a lint command when the files don't exist on disk.

    The planner generates lint commands referencing files from its *plan*, but the
    implementer may create a different file structure.  Running ``py_compile`` on
    non-existent files produces ``FileNotFoundError``, which the repair agent
    cannot fix — causing an infinite validate/repair loop.

    This function checks each source-file token against the filesystem and removes
    tokens whose files don't exist.  Works for both serial and parallel pipelines.

    When *owned_files* is provided (parallel pipeline), the fallback discovery
    is restricted to owned files only — prevents linting other modules' files
    that the repair agent cannot modify.
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
            # All file tokens were non-existent.  Find replacement files.
            if owned_files:
                # Parallel pipeline: only lint the module's own files.
                actual_files = [
                    f for f in owned_files
                    if os.path.exists(os.path.join(project_root, f))
                    and not _matches_test_pattern(f)
                ]
            else:
                # Serial pipeline: discover all source files in the project.
                actual_files = []
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


_TEST_DIR_TOKENS = frozenset({"tests/", "tests", "test/", "test", "spec/", "spec"})


def _scope_test_command(
    cmd: str,
    test_files: list[str],
) -> str:
    """Rewrite a test command to run only specific test files instead of a directory.

    The planner generates a global test command like:
        source venv/bin/activate && python -m pytest tests/ -v

    In parallel pipelines, each module must run only its own test files.
    This replaces directory tokens (``tests/``, ``test/``) with the concrete
    test file paths, preserving shell prefixes and flags.

    Returns the original command unchanged if no directory tokens are found
    or if *test_files* is empty.
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


class ValidateTask(Task):
    """Runs syntax checks and tests, routing failures to repair via jump_to."""

    def execute(self, stage: StageExecution) -> TaskResult:
        start_time = time.monotonic()
        project_root = stage.context.get("project_root", os.getcwd())
        repair_attempt = stage.context.get("repair_attempt", 0)
        max_attempts = stage.context.get("max_repair_attempts", MAX_REPAIR_ATTEMPTS)
        profile_data = stage.context.get("language_profile", {})
        plan_config = stage.context.get("plan_config", {})

        # ── Jump-limit safety net ─────────────────────────────────────
        if check_jump_limit(stage.context):
            emit(
                M.VFAL,
                f"Global jump limit reached "
                f"({stage.context.get('_jump_count', 0)}/{stage.context.get('_max_jumps', 50)}). "
                f"Terminating to prevent infinite loop.",
            )
            return TaskResult.terminal(
                error="Jump limit exceeded — validate/repair loop ran too long",
            )

        # Resolve language profile: use context data, fall back to auto-detection.
        # When the build-time profile says "unknown" (pipeline was created before
        # setup ran and created manifest files), re-detect at runtime and update
        # the context so downstream stages (repair, reimplementation) also benefit.
        detected = detect_language(project_root)
        base_profile = get_profile(detected)
        if profile_data.get("language") == "unknown" and detected != "unknown":
            profile_data = base_profile.to_dict()
            stage.context["language_profile"] = profile_data
            logger.debug("Re-detected language as %s (was unknown)", detected)

        # Auto-detect test files when not provided.
        # In parallel pipelines (owned_files is set), derive module-scoped
        # test files from owned source files to avoid cross-module interference.
        # In serial pipelines, discover all test files for the deny list.
        if not stage.context.get("test_files"):
            extensions = tuple(profile_data.get("extensions", base_profile.extensions))
            skip_dirs = tuple(profile_data.get("skip_dirs", base_profile.skip_dirs))
            discovered = _discover_test_files(project_root, extensions, skip_dirs)
            if discovered:
                owned_files = stage.context.get("owned_files")
                if owned_files:
                    # Parallel pipeline: scope to module's test files only
                    scoped = _derive_module_test_files(discovered, owned_files)
                    if scoped:
                        stage.context["test_files"] = scoped
                        logger.debug(
                            "Derived %d module-scoped test files from %d owned files",
                            len(scoped),
                            len(owned_files),
                        )
                    else:
                        logger.warning(
                            "Could not derive test files for module (owned=%s). Running without test scoping.",
                            owned_files,
                        )
                else:
                    # Serial pipeline: use all test files
                    stage.context["test_files"] = discovered
                    logger.debug("Auto-detected %d test files", len(discovered))

        def _emit_elapsed() -> None:
            elapsed = time.monotonic() - start_time
            emit(M.SELP, f"{elapsed:.1f}s")

        # Prefer planner-decided test command over profile default.
        # In parallel pipelines, scope test commands to module-specific test files
        # to prevent cross-module test interference.
        plan_test_cmd = plan_config.get("test_command") if plan_config else None
        if plan_test_cmd:
            test_cmd = _parse_command(plan_test_cmd)
        else:
            test_cmd = tuple(profile_data.get("test_command", base_profile.test_command))
        syntax_cmd_raw = profile_data.get("syntax_check_command")
        syntax_cmd = tuple(syntax_cmd_raw) if syntax_cmd_raw is not None else base_profile.syntax_check_command
        lang_name = profile_data.get("language", base_profile.language)
        module_name = stage.context.get("module_name", "")

        # Resolve module-scoped test files.  The planner may specify test_files
        # that don't actually exist (the test writer may create different names).
        # When that happens in parallel mode, auto-derive from actual test files.
        scoped_test_files = stage.context.get("test_files")
        owned_files_for_tests = stage.context.get("owned_files")
        if scoped_test_files:
            existing = [f for f in scoped_test_files if os.path.exists(os.path.join(project_root, f))]
            if existing:
                # Planner's test files exist — use them
                if plan_test_cmd and owned_files_for_tests:
                    # Parallel mode: scope test command to specific files
                    plan_test_cmd_scoped = _scope_test_command(plan_test_cmd, existing)
                    test_cmd = _parse_command(plan_test_cmd_scoped)
                else:
                    test_cmd = (*test_cmd, *existing)
            elif owned_files_for_tests:
                # Parallel mode: planner's test files don't exist.
                # Auto-derive from actually existing test files.
                extensions = tuple(profile_data.get("extensions", base_profile.extensions))
                skip_dirs = tuple(profile_data.get("skip_dirs", base_profile.skip_dirs))
                discovered = _discover_test_files(project_root, extensions, skip_dirs)
                derived = _derive_module_test_files(discovered, owned_files_for_tests)
                if derived:
                    logger.info(
                        "Planner test files missing (%s); auto-derived %d test file(s) for module",
                        scoped_test_files, len(derived),
                    )
                    if plan_test_cmd:
                        plan_test_cmd_scoped = _scope_test_command(plan_test_cmd, derived)
                        test_cmd = _parse_command(plan_test_cmd_scoped)
                    else:
                        test_cmd = (*test_cmd, *derived)
                else:
                    logger.warning(
                        "No test files found for module (owned=%s, planned=%s). "
                        "Skipping test run for this module.",
                        owned_files_for_tests, scoped_test_files,
                    )
                    # Replace test command with a no-op so we don't run
                    # the global test suite for this module.
                    test_cmd = ("true",)

        emit(
            M.VRUN,
            f"ValidateTask running [{lang_name}] (attempt {repair_attempt}/{max_attempts}) in {project_root}",
            label=module_name,
        )

        # Resolve lint-check commands: prefer planner-provided lint_command
        # (which may activate a venv), fall back to profile defaults.
        # Two layers of protection against stale file references:
        # 1. Parallel pipelines: scope to owned files only
        # 2. All pipelines: strip file tokens that don't exist on disk
        plan_lint_cmd = plan_config.get("lint_command") if plan_config else None
        if plan_lint_cmd:
            owned = stage.context.get("owned_files")
            if owned:
                plan_lint_cmd = _scope_lint_command(plan_lint_cmd, owned)
            plan_lint_cmd = _strip_nonexistent_files(plan_lint_cmd, project_root, owned_files=owned)
            lint_cmds = [_parse_command(plan_lint_cmd)]
        else:
            lint_check_raw = profile_data.get("lint_check_commands", ())
            if not lint_check_raw:
                lint_check_raw = base_profile.lint_check_commands
            lint_cmds = [_parse_command(c) for c in lint_check_raw] if lint_check_raw else []

        # Detect source root layout and build env for subprocess calls.
        test_env = _build_test_env(project_root, profile_data)

        syntax_result = self._check_syntax(project_root, syntax_cmd, env=test_env)
        if syntax_result is not None:
            return self._handle_failure(
                stage,
                syntax_result,
                repair_attempt,
                max_attempts,
                "syntax",
                profile_data,
            )

        owned_files = stage.context.get("owned_files")
        lint_result = self._check_lint(project_root, lint_cmds, env=test_env, owned_files=owned_files)
        if lint_result is not None:
            return self._handle_failure(
                stage,
                lint_result,
                repair_attempt,
                max_attempts,
                "lint",
                profile_data,
            )

        test_result = self._run_tests(project_root, test_cmd, env=test_env)
        if test_result["passed"]:
            mod_label = f" ({module_name})" if module_name else ""
            emit(M.VPAS, f"All tests passed!{mod_label} ({test_result.get('total', 0)} tests)", label=module_name or "")
            return TaskResult.success(
                outputs={
                    "tests_passed": True,
                    "test_output": test_result["output"][:TEST_OUTPUT_LIMIT],
                    "total_tests": test_result.get("total", 0),
                    "repair_attempts_used": repair_attempt,
                }
            )

        return self._handle_failure(
            stage,
            test_result["output"],
            repair_attempt,
            max_attempts,
            "test",
            profile_data,
        )

    def _handle_failure(
        self,
        stage: StageExecution,
        output: str,
        attempt: int,
        max_attempts: int,
        failure_type: str,
        profile_data: dict[str, Any],
    ) -> TaskResult:
        previous = stage.context.get("previous_failures", [])
        summary = output[:500]
        updated_failures = previous + [summary]
        module_name = stage.context.get("module_name", "")
        mod_tag = f" [{module_name}]" if module_name else ""

        if attempt >= max_attempts:
            reimpl_count = stage.context.get("reimplementation_count", 0)
            max_reimpl = stage.context.get("max_reimplementations", MAX_REIMPLEMENTATIONS)

            if reimpl_count < max_reimpl:
                emit(
                    M.VFAL,
                    f"Repair exhausted ({max_attempts} attempts).{mod_tag} "
                    f"Re-implementing from scratch "
                    f"(reimplementation {reimpl_count + 1}/{max_reimpl})",
                )
                repair_history = previous[-max_attempts:]
                reimpl_context: dict[str, Any] = {
                    "previous_test_failures": output[:TEST_OUTPUT_LIMIT],
                    "repair_history": repair_history,
                    "failure_summary": (
                        f"After {max_attempts} repair attempts, "
                        f"these tests still fail:\n{output[:2000]}\n\n"
                        f"Repair attempts tried:\n"
                        + "\n---\n".join(f"Attempt {i + 1}: {f}" for i, f in enumerate(repair_history))
                    ),
                    "project_root": stage.context.get("project_root", os.getcwd()),
                    "language_profile": profile_data,
                }
                propagate_context(stage.context, reimpl_context)
                # Set after propagate to prevent stale values from overwriting.
                reimpl_context["repair_attempt"] = 0
                reimpl_context["reimplementation_count"] = reimpl_count + 1
                increment_jump_count(reimpl_context)
                return TaskResult.jump_to(
                    stage.context.get("jump_implement_ref", "implement"),
                    context=reimpl_context,
                )

            emit(
                M.VFAL,
                f"All reimplementation attempts exhausted{mod_tag} "
                f"({max_reimpl} reimplementations × {max_attempts} repairs). "
                f"Pipeline FAILED.",
            )
            return TaskResult.terminal(
                error=(
                    f"Tests still failing after {max_reimpl} reimplementations "
                    f"× {max_attempts} repairs = "
                    f"{max_reimpl * max_attempts} total attempts"
                ),
            )

        emit(
            M.VFAL,
            f"{failure_type} failure detected.{mod_tag} Jumping to repair (attempt {attempt + 1}/{max_attempts})",
        )
        emit_block(M.VTST, f"Failure output ({failure_type})", output[:2000], max_lines=40)

        repair_context: dict[str, Any] = {
            "_repair_requested": True,
            "test_output": output[:TEST_OUTPUT_LIMIT],
            "tests_passed": False,
            "tests_partial": False,
            "previous_failures": updated_failures[-5:],
            "failure_type": failure_type,
            "project_root": stage.context.get("project_root", os.getcwd()),
            "spec_id": stage.context.get("spec_id"),
            "language_profile": profile_data,
        }
        propagate_context(stage.context, repair_context)
        # Set repair_attempt AFTER propagate_context to prevent stale
        # stage.context["repair_attempt"] from overwriting the increment.
        repair_context["repair_attempt"] = attempt + 1
        increment_jump_count(repair_context)
        return TaskResult.jump_to(
            stage.context.get("jump_repair_ref", "repair"),
            context=repair_context,
        )

    @staticmethod
    def _check_syntax(
        project_root: str,
        syntax_cmd: tuple[str, ...] | None,
        env: dict[str, str] | None = None,
    ) -> str | None:
        """Run syntax check using the profile's syntax_check_command."""
        if syntax_cmd is None:
            return None

        try:
            result = subprocess.run(
                list(syntax_cmd),
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
            if result.returncode != 0:
                return f"Syntax check failed:\n{result.stdout}\n{result.stderr}"
        except subprocess.TimeoutExpired:
            return "Syntax check timed out after 120s"
        except FileNotFoundError:
            logger.warning("Syntax check tool not found: %s", syntax_cmd[0])
            return None
        except Exception as e:
            return f"Syntax check error: {e}"

        return None

    @staticmethod
    def _check_lint(
        project_root: str,
        lint_cmds: list[tuple[str, ...]],
        env: dict[str, str] | None = None,
        owned_files: list[str] | None = None,
    ) -> str | None:
        """Run lint-check commands from the language profile.

        Returns combined lint error output on failure, or None if all pass.
        Skips gracefully when the lint tool is not installed.
        """
        if not lint_cmds:
            return None

        errors: list[str] = []
        for cmd in lint_cmds:
            try:
                result = subprocess.run(
                    list(cmd),
                    cwd=project_root,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=env,
                )
                if result.returncode != 0:
                    output = (result.stdout + "\n" + result.stderr).strip()
                    # Treat "No module named X" as tool-not-installed, not lint error.
                    # This prevents false lint failures when e.g. ruff is not installed.
                    if "No module named" in output:
                        logger.warning("Lint module not installed: %s", " ".join(cmd[:3]))
                        continue
                    # Strip errors in test files and unowned files — the repairer
                    # cannot modify them, creating an unwinnable repair cycle.
                    output = _filter_test_file_lint(output, owned_files=owned_files)
                    if not output:
                        logger.info("All lint errors are in test files — treating as clean.")
                        continue
                    errors.append(f"Lint check failed ({' '.join(cmd[:2])}):\n{output}")
            except subprocess.TimeoutExpired:
                errors.append(f"Lint check timed out after 120s ({' '.join(cmd[:2])})")
            except FileNotFoundError:
                logger.warning("Lint tool not found: %s", cmd[0])
                continue
            except Exception as e:
                errors.append(f"Lint check error ({' '.join(cmd[:2])}): {e}")

        return "\n\n".join(errors) if errors else None

    @staticmethod
    def _run_tests(
        project_root: str,
        test_cmd: tuple[str, ...],
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            result = subprocess.run(
                list(test_cmd),
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return {
                "passed": False,
                "output": f"Tests timed out after 120s (cmd: {' '.join(test_cmd)})",
                "total": 0,
            }
        except FileNotFoundError:
            return {
                "passed": False,
                "output": f"Test runner not found: {test_cmd[0]}",
                "total": 0,
            }
        except Exception as e:
            return {"passed": False, "output": f"Test execution error: {e}", "total": 0}

        output = result.stdout + "\n" + result.stderr
        passed = result.returncode == 0
        total = _count_tests(output)
        return {"passed": passed, "output": output, "total": total}


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
