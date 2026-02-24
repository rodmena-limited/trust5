import difflib
import glob
import json
import logging
import os
import re
import shlex
import subprocess
from typing import Any

from .init import ProjectInitializer
from .message import M, emit, emit_block
from .tool_definitions import build_ask_user_definition, build_tool_definitions

logger = logging.getLogger(__name__)

# Destructive command patterns blocked when executed by LLM agents.
# These are regex patterns matched against the full command string.
_BLOCKED_COMMAND_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+-[^\s]*r[^\s]*f", re.IGNORECASE),  # rm -rf, rm -fr, etc.
    re.compile(r"\brm\s+-[^\s]*f[^\s]*r", re.IGNORECASE),  # rm -fr variants
    re.compile(r"\bmkfs\b", re.IGNORECASE),
    re.compile(r"\bdd\s+", re.IGNORECASE),
    re.compile(r"\bchmod\s+777\b"),
    re.compile(r"\bchmod\s+-R\s+777\b"),
    re.compile(r">\s*/dev/sd[a-z]"),
    re.compile(r"\b:(){ :\|:& };:", re.IGNORECASE),  # fork bomb
    re.compile(r"\bcurl\b.*\|\s*(?:bash|sh|zsh)\b"),  # curl | bash
    re.compile(r"\bwget\b.*\|\s*(?:bash|sh|zsh)\b"),  # wget | bash
    re.compile(r"\bsqlite3\s+.*\.trust5/"),  # Accessing trust5 internal DB crashes pipeline
    # Block ANY write operation targeting .trust5/ directory.
    # This is CRITICAL: an LLM redirect like `> .trust5/trust5.db` truncates the
    # pipeline database to 0 bytes, causing unrecoverable corruption.
    re.compile(r">+\s*\.trust5/"),  # Redirect to .trust5/ (> or >>)
    re.compile(r">+\s*[^\s]*\.trust5/"),  # Redirect with path prefix
    re.compile(r"\btee\b.*\.trust5/"),  # tee to .trust5/
    re.compile(r"\bmv\b.*\.trust5/"),  # mv into .trust5/
    re.compile(r"\bcp\b.*\.trust5/"),  # cp into .trust5/
    re.compile(r"\brm\b.*\.trust5/"),  # rm inside .trust5/
    re.compile(r"\btruncate\b.*\.trust5/"),  # truncate .trust5/ files
    re.compile(r"\bcat\b.*>.*\.trust5/"),  # cat > .trust5/
]

# Safe compound command patterns that override blocked checks.
# These are standard, scoped operations that appear destructive at the regex
# level but are safe in context (e.g., find -exec rm only deletes matched files,
# find -delete only removes found entries).
_SAFE_COMMAND_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bfind\b\s+.+-exec\s+rm\b"),  # find ... -exec rm ... is scoped
    re.compile(r"\bfind\b\s+.+-delete\b"),  # find ... -delete is scoped
    re.compile(r"\bfind\b\s+.+-name\b.*-delete\b"),  # find . -name '*.pyc' -delete
    re.compile(r"\bfind\b\s+.+-type\s+\w\s+-exec\s+rm"),  # find . -type d -exec rm -rf {} +
]

# Regex for validating Python package names (allows extras and version specifiers)
_VALID_PACKAGE_RE = re.compile(r"^[a-zA-Z0-9._-]+[a-zA-Z0-9._\-\[\]>=<,! ]*$")



# ── .trust5/ directory protection ─────────────────────────────────────
# Path segments that indicate the file is inside the Trust5 internal
# state directory.  Used by both Write/Edit tools and Bash tool to
# prevent LLM agents from accidentally (or intentionally) corrupting
# the pipeline database.
_TRUST5_DIR_MARKERS = (
    f"{os.sep}.trust5{os.sep}",
    f"{os.sep}.trust5",  # path ending with .trust5 (the directory itself)
)


def _is_trust5_internal_path(path: str) -> bool:
    """Return True if *path* resolves inside a ``.trust5/`` directory.

    Checks both the raw path and its normalized form to catch symlink
    bypasses (e.g. ``/tmp`` -> ``/private/tmp`` on macOS).
    """
    # Normalize to absolute for reliable matching
    normalized = os.path.normpath(os.path.abspath(path))
    for marker in _TRUST5_DIR_MARKERS:
        if marker in normalized or normalized.endswith(marker):
            return True
    return False

def _is_project_scoped_rm(command: str, workdir: str) -> bool:
    """Allow ``rm -rf`` when ALL targets resolve within the project directory.

    Agents legitimately need to remove directories during reimplementation
    (e.g. ``rm -rf celery_core/``).  This function parses the rm targets and
    verifies every one resolves strictly *inside* ``workdir``.

    Returns False (unsafe) for:
    - Bare ``rm -rf`` with no targets
    - Targets that resolve outside workdir (``..``, ``/``, ``~``, env vars)
    - Targets that resolve to workdir itself (``rm -rf .``)
    - Unparseable commands
    """
    if not re.search(r"\brm\s+-[^\s]*r", command):
        return False

    abs_workdir = os.path.realpath(workdir)

    try:
        parts = shlex.split(command)
    except ValueError:
        return False

    # Find the 'rm' token
    rm_idx = None
    for i, part in enumerate(parts):
        if part == "rm" or part.endswith("/rm"):
            rm_idx = i
            break
    if rm_idx is None:
        return False

    # Collect non-flag arguments until a shell operator
    targets: list[str] = []
    for part in parts[rm_idx + 1 :]:
        if part.startswith("-"):
            continue
        if part in ("&&", "||", ";", "|"):
            break
        targets.append(part)

    if not targets:
        return False

    for target in targets:
        # Reject shell expansions we can't resolve statically
        if target.startswith("~") or target.startswith("$"):
            return False
        resolved = os.path.realpath(os.path.join(abs_workdir, target))
        # Must be strictly inside workdir (not workdir itself)
        if not resolved.startswith(abs_workdir + os.sep):
            return False

    return True


_TEST_FILE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(^|/)test_[^/]+$"),  # test_foo.py
    re.compile(r"(^|/)[^/]+_test\.[^/]+$"),  # foo_test.py, foo_test.go
    re.compile(r"(^|/)tests/"),  # tests/ directory
    re.compile(r"(^|/)test/"),  # test/ directory (Maven, Gradle)
    re.compile(r"(^|/)spec/"),  # spec/ directory
    re.compile(r"(^|/)__tests__/"),  # __tests__/ directory (Jest)
    re.compile(r"(^|/)[^/]+_spec\.[^/]+$"),  # foo_spec.rb
    re.compile(r"(^|/)conftest\.py$"),  # pytest conftest.py
    re.compile(r"(^|/)[^/]+\.test\.[^/]+$"),  # foo.test.ts, foo.test.js (Jest/Vitest)
    re.compile(r"(^|/)Test[A-Z][^/]*\.java$"),  # TestFoo.java (JUnit)
]


def _matches_test_pattern(path: str) -> bool:
    """Check if a file path matches common test file patterns."""
    for pattern in _TEST_FILE_PATTERNS:
        if pattern.search(path):
            return True
    return False


class Tools:
    """File system and shell tools available to LLM agents.

    Provides Read, Write, Edit, Bash, Glob, Grep, and package installation.
    Enforces file ownership (allowlist/denylist) and test-file write protection.
    """

    _non_interactive: bool = False

    def __init__(
        self,
        owned_files: list[str] | None = None,
        denied_files: list[str] | None = None,
        deny_test_patterns: bool = False,
    ) -> None:
        self._owned_files: set[str] | None = None
        if owned_files:
            self._owned_files = {os.path.realpath(f) for f in owned_files}
        self._denied_files: set[str] | None = None
        if denied_files:
            self._denied_files = {os.path.realpath(f) for f in denied_files}
        self._deny_test_patterns = deny_test_patterns

    @classmethod
    def set_non_interactive(cls, value: bool = True) -> None:
        cls._non_interactive = value

    @classmethod
    def is_non_interactive(cls) -> bool:
        return cls._non_interactive

    def _check_write_allowed(self, file_path: str) -> str | None:
        real_path = os.path.realpath(file_path)
        # Layer 0: .trust5/ directory protection — ABSOLUTE block.
        # The .trust5/ directory contains the pipeline database, logs,
        # and event socket.  An LLM agent writing here (even accidentally)
        # can truncate the database to 0 bytes, causing unrecoverable
        # pipeline corruption.  This check uses BOTH path forms to prevent
        # symlink-based bypasses (e.g. /tmp -> /private/tmp on macOS).
        if _is_trust5_internal_path(file_path) or _is_trust5_internal_path(real_path):
            emit(M.SWRN, f"BLOCKED write to Trust5 internal path: {file_path}")
            return (
                f"BLOCKED: Write to {file_path} denied — this path is inside the .trust5/ "
                "directory which contains the pipeline database and internal state. "
                "Writing here would corrupt the running pipeline."
            )
        # Layer 1: Explicit denylist — hard block, checked first
        if self._denied_files and real_path in self._denied_files:
            return (
                f"BLOCKED: Write to {real_path} denied — file is in denied_files "
                f"(test files are read-only for this agent)."
            )

        # Layer 2: Pattern-based test file blocking
        if self._deny_test_patterns and _matches_test_pattern(real_path):
            return (
                f"BLOCKED: Write to {real_path} denied — matches test file pattern. "
                f"Test files are read-only for implementer/repairer agents."
            )

        # Layer 3: Owned-files allowlist
        if self._owned_files is None:
            return None
        if real_path in self._owned_files:
            return None
        owned_list = sorted(self._owned_files)
        return (
            f"BLOCKED: Write to {real_path} denied — this file is owned by another module. "
            f"Do NOT attempt to modify files outside your ownership. "
            f"Instead, write your implementation from scratch into YOUR files: {owned_list}"
        )

    @staticmethod
    def init_project(path: str = ".") -> str:
        """Initializes a new project."""
        try:
            from .lang import detect_language

            initializer = ProjectInitializer(path)
            initializer._setup_structure()
            detected = detect_language(path)
            initializer._write_default_config(detected)
            return "Project initialized successfully."
        except Exception as e:
            return f"Error initializing project: {str(e)}"

    @staticmethod
    def read_file(
        file_path: str,
        offset: int | None = None,
        limit: int | None = None,
    ) -> str:
        """Reads a file from the local filesystem.

        When offset/limit are provided, returns only the specified line range.
        Lines are 1-indexed (offset=1 is the first line). Without offset/limit,
        returns the full file content.
        """
        try:
            with open(file_path, encoding="utf-8") as f:
                if offset is not None or limit is not None:
                    lines = f.readlines()
                    total = len(lines)
                    start = max(0, (offset or 1) - 1)  # 1-indexed to 0-indexed
                    end = start + limit if limit else total
                    selected = lines[start:end]
                    header = f"[Lines {start + 1}-{min(end, total)} of {total}]\n"
                    return header + "".join(selected)
                return f.read()
        except Exception as e:
            return f"Error reading file {file_path}: {str(e)}"

    def write_file(self, file_path: str, content: str) -> str:
        try:
            real_path = os.path.realpath(file_path)
            blocked = self._check_write_allowed(real_path)
            if blocked:
                return blocked
            old_content = None
            if os.path.exists(real_path):
                try:
                    with open(real_path, encoding="utf-8") as f:
                        old_content = f.read()
                except Exception:
                    logger.debug("Failed to read existing file content at %s", real_path, exc_info=True)

            emit(M.TWRT, f"Writing {len(content)} chars to {real_path}")
            os.makedirs(os.path.dirname(real_path), exist_ok=True)
            with open(real_path, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())

            if old_content is not None and old_content != content:
                diff = difflib.unified_diff(
                    old_content.splitlines(),
                    content.splitlines(),
                    fromfile=f"a/{file_path}",
                    tofile=f"b/{file_path}",
                    lineterm="",
                )
                emit_block(M.KDIF, f"PATCH {file_path}", "\n".join(diff), max_lines=60)
            else:
                emit_block(
                    M.KCOD,
                    f"NEW {file_path} ({len(content)} chars)",
                    content,
                    max_lines=60,
                )

            action = "modified" if old_content is not None else "created"
            emit(M.FCHG, f"path={real_path} action={action}")
            return f"Successfully wrote to {file_path}"
        except Exception as e:
            return f"Error writing file {file_path}: {str(e)}"

    @staticmethod
    def read_files(file_paths: list[str]) -> str:
        results: dict[str, str] = {}
        for fp in file_paths:
            try:
                with open(fp, encoding="utf-8") as f:
                    results[fp] = f.read()
            except Exception as e:
                results[fp] = f"Error: {e}"
        return json.dumps(results, ensure_ascii=False)

    def edit_file(self, file_path: str, old_string: str, new_string: str) -> str:
        real_path = os.path.realpath(file_path)
        blocked = self._check_write_allowed(real_path)
        if blocked:
            return blocked
        try:
            with open(real_path, encoding="utf-8") as f:
                content = f.read()
        except FileNotFoundError:
            return f"Error: file not found: {real_path}"
        except Exception as e:
            return f"Error reading {real_path}: {e}"

        count = content.count(old_string)
        if count == 0:
            return f"Error: old_string not found in {file_path}"
        if count > 1:
            return f"Error: old_string found {count} times in {file_path}. Provide more context to make it unique."

        new_content = content.replace(old_string, new_string, 1)
        try:
            with open(real_path, "w", encoding="utf-8") as f:
                f.write(new_content)
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            return f"Error writing {real_path}: {e}"

        diff = difflib.unified_diff(
            content.splitlines(),
            new_content.splitlines(),
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            lineterm="",
        )
        emit_block(M.KDIF, f"EDIT {file_path}", "\n".join(diff), max_lines=60)
        emit(M.FCHG, f"path={real_path} action=edited")
        return f"Successfully edited {file_path}"

    @staticmethod
    def run_bash(command: str, workdir: str = ".") -> str:
        """Executes a bash command with destructive-pattern blocklist.

        Automatically activates project virtualenv if .venv or venv exists
        in the workdir, preventing pollution of Trust5's own environment.
        """
        is_safe_context = any(p.search(command) for p in _SAFE_COMMAND_PATTERNS)
        if not is_safe_context:
            is_safe_context = _is_project_scoped_rm(command, workdir)
        if not is_safe_context:
            for pattern in _BLOCKED_COMMAND_PATTERNS:
                if pattern.search(command):
                    emit(M.SWRN, f"BLOCKED dangerous command: {command[:200]}")
                    return f"Error: command blocked by safety filter. Pattern matched: {pattern.pattern}"
        try:
            # Build environment with venv activation if present
            env = os.environ.copy()
            for venv_dir in (".venv", "venv"):
                venv_bin = os.path.join(workdir, venv_dir, "bin")
                if os.path.isdir(venv_bin):
                    env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"
                    env["VIRTUAL_ENV"] = os.path.join(os.path.abspath(workdir), venv_dir)
                    env.pop("PYTHONHOME", None)
                    logger.debug("Activated venv at %s for bash command", venv_bin)
                    break

            result = subprocess.run(
                command,
                shell=True,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
            return f"Stdout:\n{result.stdout}\nStderr:\n{result.stderr}\nExit Code: {result.returncode}"
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after 120s: {command[:200]}"
        except (OSError, subprocess.SubprocessError) as e:
            return f"Error running command '{command[:200]}': {e}"

    @staticmethod
    def list_files(pattern: str, workdir: str = ".") -> list[str]:
        """Lists files matching a glob pattern."""
        try:
            files = glob.glob(os.path.join(workdir, pattern), recursive=True)
            return [os.path.relpath(f, workdir) for f in files]
        except Exception as e:
            return [f"Error listing files: {str(e)}"]

    @staticmethod
    def grep_files(pattern: str, path: str = ".", include: str = "*") -> str:
        """Search files using grep with safe argument passing (no shell interpolation)."""
        try:
            cmd = ["grep", "-r", pattern, path, f"--include={include}"]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )
            return f"Stdout:\n{result.stdout}\nStderr:\n{result.stderr}\nExit Code: {result.returncode}"
        except subprocess.TimeoutExpired:
            return "Error: grep timed out after 60s"
        except (OSError, subprocess.SubprocessError) as e:
            return f"Error running grep: {e}"

    @staticmethod
    def install_package(package_name: str, install_prefix: str = "") -> str:
        """Install a package using the project's package manager."""
        if not _VALID_PACKAGE_RE.match(package_name):
            return f"Error: invalid package name: {package_name!r}"
        if not install_prefix:
            return f"Error: no install command configured. Cannot install {package_name!r}."
        return Tools.run_bash(f"{shlex.quote(install_prefix)} {shlex.quote(package_name)}")

    @classmethod
    def get_definitions(
        cls,
        *,
        non_interactive: bool = False,
        allowed_tools: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Returns tool definitions for LLM.

        When non_interactive=True, AskUserQuestion is excluded entirely
        so the LLM never attempts to call it during autonomous pipelines.

        When allowed_tools is provided, only tools whose names appear in
        the list are returned.  This is used to sandbox agents (e.g. the
        planner gets only read-only tools).
        """
        defs = build_tool_definitions()

        # Only include AskUserQuestion when running interactively
        if not non_interactive and not cls._non_interactive:
            defs.append(build_ask_user_definition())

        if allowed_tools is not None:
            allowed_set = set(allowed_tools)
            defs = [d for d in defs if d.get("function", {}).get("name") in allowed_set]

        return defs

    @classmethod
    def ask_user(cls, question: str, options: list[str] = []) -> str:
        import sys

        default = options[0] if options else "yes"

        if cls._non_interactive:
            emit(M.UAUT, f"Auto: {question} -> {default}")
            return default

        if not sys.stdin.isatty():
            emit(M.UAUT, f"Auto (no tty): {question} -> {default}")
            return default

        emit(M.UASK, f"{question}")

        if options:
            for i, opt in enumerate(options):
                print(f"{i + 1}. {opt}", flush=True)
            try:
                sys.stdout.flush()
                choice = input("Enter choice number (default 1): ").strip()
                if not choice:
                    return options[0]
                idx = int(choice) - 1
                if 0 <= idx < len(options):
                    return options[idx]
                return options[0]
            except (ValueError, EOFError):
                return options[0]
        else:
            try:
                sys.stdout.flush()
                answer = input("Answer (default: yes): ").strip()
                return answer if answer else "yes"
            except EOFError:
                return "yes"
