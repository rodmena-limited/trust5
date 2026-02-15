import difflib
import glob
import json
import os
import re
import shlex
import subprocess
from typing import Any
from .init import ProjectInitializer
from .message import M, emit, emit_block
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
]
_SAFE_COMMAND_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bfind\b\s+.+-exec\s+rm\b"),  # find ... -exec rm ... is scoped
    re.compile(r"\bfind\b\s+.+-delete\b"),  # find ... -delete is scoped
]
_VALID_PACKAGE_RE = re.compile(r"^[a-zA-Z0-9._-]+[a-zA-Z0-9._\-\[\]>=<,! ]*$")
_TEST_FILE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(^|/)test_[^/]+$"),  # test_foo.py
    re.compile(r"(^|/)[^/]+_test\.[^/]+$"),  # foo_test.py, foo_test.go
    re.compile(r"(^|/)tests/"),  # tests/ directory
    re.compile(r"(^|/)spec/"),  # spec/ directory
    re.compile(r"(^|/)[^/]+_spec\.[^/]+$"),  # foo_spec.rb
]

def _matches_test_pattern(path: str) -> bool:
    """Check if a file path matches common test file patterns."""
    for pattern in _TEST_FILE_PATTERNS:
        if pattern.search(path):
            return True
    return False

class Tools:
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

    def set_non_interactive(cls, value: bool = True) -> None:
        cls._non_interactive = value

    def is_non_interactive(cls) -> bool:
        return cls._non_interactive

    def _check_write_allowed(self, file_path: str) -> str | None:
        real_path = os.path.realpath(file_path)

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
        return (
            f"BLOCKED: Write to {real_path} denied — file not in owned_files. "
            f"This module may only write to: {sorted(self._owned_files)}"
        )

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
                    pass

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

    def run_bash(command: str, workdir: str = ".") -> str:
        """Executes a bash command with destructive-pattern blocklist."""
        is_safe_context = any(p.search(command) for p in _SAFE_COMMAND_PATTERNS)
        if not is_safe_context:
            for pattern in _BLOCKED_COMMAND_PATTERNS:
                if pattern.search(command):
                    emit(M.SWRN, f"BLOCKED dangerous command: {command[:200]}")
                    return f"Error: command blocked by safety filter. Pattern matched: {pattern.pattern}"
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=120,
            )
            return f"Stdout:\n{result.stdout}\nStderr:\n{result.stderr}\nExit Code: {result.returncode}"
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after 120s: {command[:200]}"
        except (OSError, subprocess.SubprocessError) as e:
            return f"Error running command '{command[:200]}': {e}"

    def list_files(pattern: str, workdir: str = ".") -> list[str]:
        """Lists files matching a glob pattern."""
        try:
            files = glob.glob(os.path.join(workdir, pattern), recursive=True)
            return [os.path.relpath(f, workdir) for f in files]
        except Exception as e:
            return [f"Error listing files: {str(e)}"]
