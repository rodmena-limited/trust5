"""Trust5 Watchdog — autonomous pipeline health monitor."""

import logging
import os
import re
import time
from typing import Any

from stabilize import StageExecution, Task, TaskResult

from ..core.message import M, emit

logger = logging.getLogger(__name__)

# Garbled file pattern (shell redirect artifacts like "=3.0.0")
_GARBLED_RE = re.compile(r"^=[0-9]")

# Double extension pattern (e.g. "config.toml.py")
_DOUBLE_EXT_RE = re.compile(r"\.\w+\.\w+$")

# Known legitimate double extensions to ignore
_LEGIT_DOUBLE_EXT = frozenset(
    {
        ".spec.ts",
        ".spec.js",
        ".test.ts",
        ".test.js",
        ".test.tsx",
        ".test.jsx",
        ".spec.tsx",
        ".spec.jsx",
        ".d.ts",
        ".config.js",
        ".config.ts",
        ".config.mjs",
        ".module.ts",
        ".module.css",
        ".stories.tsx",
        ".min.js",
        ".min.css",
        ".map.js",
        ".setup.ts",
        ".setup.js",
    }
)

# Stub content indicators
_STUB_INDICATORS = (
    "implementation required",
    "# Module:",
    "// Module:",
    '"""Module:',
)

_SKIP_DIRS = frozenset(
    {
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
        ".tox",
        ".nox",
    }
)

# Source extensions to check for stubs/emptiness
_SOURCE_EXTS = frozenset(
    {
        ".py",
        ".go",
        ".ts",
        ".js",
        ".tsx",
        ".jsx",
        ".rs",
        ".java",
        ".rb",
        ".ex",
        ".exs",
        ".cpp",
        ".c",
        ".h",
    }
)


class WatchdogTask(Task):
    """Autonomous pipeline health monitor.

    Runs periodic checks for file system anomalies, context issues,
    and pipeline health problems. Reports findings via TUI events.
    """

    # How long to wait between check cycles (seconds)
    CHECK_INTERVAL = 12
    # Maximum runtime (prevent infinite running if pipeline hangs)
    MAX_RUNTIME = 7200  # 2 hours

    def execute(self, stage: StageExecution) -> TaskResult:
        project_root = stage.context.get("project_root", os.getcwd())
        language_profile = stage.context.get("language_profile", {})

        emit(M.WDST, "Watchdog started — monitoring pipeline health")

        start_time = time.monotonic()
        check_count = 0
        total_warnings = 0
        total_errors = 0

        try:
            while True:
                elapsed = time.monotonic() - start_time
                if elapsed > self.MAX_RUNTIME:
                    emit(M.WDWN, f"Watchdog max runtime reached ({self.MAX_RUNTIME}s). Stopping.")
                    break

                check_count += 1
                warnings, errors = self._run_checks(project_root, language_profile, stage.context)
                total_warnings += warnings
                total_errors += errors

                if warnings == 0 and errors == 0 and check_count % 5 == 0:
                    # Emit periodic OK every 5th clean check (~60s)
                    emit(M.WDOK, f"Check #{check_count} — all clear ({elapsed:.0f}s elapsed)")

                time.sleep(self.CHECK_INTERVAL)
        except Exception as e:
            emit(M.WDER, f"Watchdog crashed: {e}")
            logger.exception("Watchdog task crashed")

        emit(M.WDST, f"Watchdog stopped after {check_count} checks ({total_warnings} warnings, {total_errors} errors)")

        return TaskResult.success(
            outputs={
                "watchdog_checks": check_count,
                "watchdog_warnings": total_warnings,
                "watchdog_errors": total_errors,
            }
        )

    def _run_checks(
        self,
        project_root: str,
        profile: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[int, int]:
        """Run all monitoring checks. Returns (warnings, errors)."""
        warnings = 0
        errors = 0

        # File system checks
        w, e = self._check_garbled_files(project_root)
        warnings += w
        errors += e

        w, e = self._check_manifest_files(project_root, profile)
        warnings += w
        errors += e

        w, e = self._check_corrupted_extensions(project_root)
        warnings += w
        errors += e

        w, e = self._check_empty_source_files(project_root)
        warnings += w
        errors += e

        # Context quality checks
        w, e = self._check_stub_files(project_root)
        warnings += w
        errors += e

        return warnings, errors

    def _check_garbled_files(self, project_root: str) -> tuple[int, int]:
        """Check for garbled files (shell redirect artifacts)."""
        errors = 0
        try:
            for entry in os.scandir(project_root):
                if entry.is_file() and _GARBLED_RE.match(entry.name):
                    emit(
                        M.WDER,
                        f"Garbled file detected: {entry.name} (likely shell redirect artifact — should be deleted)",
                    )
                    errors += 1
        except OSError:
            pass
        return 0, errors

    def _check_manifest_files(
        self,
        project_root: str,
        profile: dict[str, Any],
    ) -> tuple[int, int]:
        """Check that required project manifest files exist."""
        required = profile.get("required_project_files", ())
        warnings = 0
        for req in required:
            full = os.path.join(project_root, req)
            if not os.path.exists(full):
                emit(M.WDWN, f"Required project file missing: {req}")
                warnings += 1
        return warnings, 0

    def _check_corrupted_extensions(self, project_root: str) -> tuple[int, int]:
        """Check for files with corrupted double extensions."""
        warnings = 0
        try:
            for entry in os.scandir(project_root):
                if not entry.is_file():
                    continue
                name = entry.name
                if _DOUBLE_EXT_RE.search(name):
                    # Check if it's a legitimate double extension
                    lower = name.lower()
                    if any(lower.endswith(legit) for legit in _LEGIT_DOUBLE_EXT):
                        continue
                    # Check if it looks like a corruption (e.g. config.toml.py)
                    emit(M.WDWN, f"Suspicious double extension: {name}")
                    warnings += 1
        except OSError:
            pass
        return warnings, 0

    def _check_empty_source_files(self, project_root: str) -> tuple[int, int]:
        """Check for empty source files that shouldn't be empty."""
        warnings = 0
        try:
            for dirpath, dirnames, filenames in os.walk(project_root):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
                for fname in filenames:
                    _, ext = os.path.splitext(fname)
                    if ext.lower() not in _SOURCE_EXTS:
                        continue
                    # __init__.py and similar can legitimately be empty
                    if fname in ("__init__.py", "mod.rs", "lib.rs"):
                        continue
                    full = os.path.join(dirpath, fname)
                    try:
                        if os.path.getsize(full) == 0:
                            rel = os.path.relpath(full, project_root)
                            emit(M.WDWN, f"Empty source file: {rel}")
                            warnings += 1
                    except OSError:
                        pass
        except OSError:
            pass
        return warnings, 0

    def _check_stub_files(self, project_root: str) -> tuple[int, int]:
        """Check for files that still contain stub/placeholder content."""
        warnings = 0
        try:
            for dirpath, dirnames, filenames in os.walk(project_root):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
                for fname in filenames:
                    _, ext = os.path.splitext(fname)
                    if ext.lower() not in _SOURCE_EXTS:
                        continue
                    if fname in ("__init__.py",):
                        continue
                    full = os.path.join(dirpath, fname)
                    try:
                        size = os.path.getsize(full)
                        if size == 0 or size > 500:
                            # Empty files caught above, large files are likely real
                            continue
                        with open(full, encoding="utf-8", errors="ignore") as f:
                            content = f.read(500)
                        lower = content.lower()
                        if any(indicator in lower for indicator in _STUB_INDICATORS):
                            rel = os.path.relpath(full, project_root)
                            emit(M.WDWN, f"Stub file still present: {rel}")
                            warnings += 1
                    except OSError:
                        pass
        except OSError:
            pass
        return warnings, 0
