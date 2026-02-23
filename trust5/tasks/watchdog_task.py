"""Trust5 Watchdog — autonomous pipeline health monitor.

Runs periodic health checks and writes a structured report to
``.trust5/watchdog_report.json`` so downstream LLM agents can
incorporate audit findings into their prompts.
"""

import json
import logging
import os
import re
import time
from typing import Any

from stabilize import StageExecution, Task, TaskResult

from ..core.message import M, emit, emit_block

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


_SENTINEL_NAME = "pipeline_complete"


def signal_pipeline_done(project_root: str) -> None:
    """Write a sentinel file to tell the watchdog the pipeline is done.

    Called by ``quality_task.py`` at every non-jump exit so the watchdog
    can terminate promptly instead of blocking until MAX_RUNTIME.
    """
    sentinel_dir = os.path.join(project_root, ".trust5")
    os.makedirs(sentinel_dir, exist_ok=True)
    sentinel = os.path.join(sentinel_dir, _SENTINEL_NAME)
    try:
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write(str(time.monotonic()))
    except OSError:
        logger.debug("Failed to write pipeline-done sentinel")


class WatchdogTask(Task):
    """Autonomous pipeline health monitor.
    and pipeline health problems.  Reports findings via TUI events
    *and* writes them to ``.trust5/watchdog_report.json`` for
    downstream LLM context injection.
    """
    # How long to wait between check cycles (seconds)
    CHECK_INTERVAL = 12
    # Maximum runtime (prevent infinite running if pipeline hangs)
    MAX_RUNTIME = 7200  # 2 hours
    # How often to emit "all clear" (every Nth clean check)
    OK_EMIT_INTERVAL = 25  # ~5 minutes at 12s interval
    def execute(self, stage: StageExecution) -> TaskResult:
        project_root = stage.context.get("project_root", os.getcwd())
        language_profile = stage.context.get("language_profile", {})
        # Remove stale sentinel from a previous pipeline run.
        self._clear_sentinel(project_root)

        emit(M.WDST, "Watchdog started \u2014 monitoring pipeline health")
        start_time = time.monotonic()
        check_count = 0
        total_warnings = 0
        total_errors = 0
        # Accumulate structured findings for the report file.
        all_findings: list[dict[str, str]] = []
        # Track last emitted findings to avoid spamming the TUI with
        # identical reports every 12 seconds.
        last_emitted_findings: list[dict[str, str]] = []
        try:
            while True:
                elapsed = time.monotonic() - start_time
                if elapsed > self.MAX_RUNTIME:
                    emit(M.WDWN, f"Watchdog max runtime reached ({self.MAX_RUNTIME}s). Stopping.")
                    break

                # Check if the pipeline signalled completion.
                if self._pipeline_done(project_root):
                    emit(
                        M.WDST,
                        f"Pipeline complete \u2014 watchdog shutting down ({elapsed:.0f}s, {check_count} checks)",
                    )
                    self._clear_sentinel(project_root)
                    break
                check_count += 1
                findings: list[dict[str, str]] = []
                warnings, errors = self._run_checks(
                    project_root,
                    language_profile,
                    stage.context,
                    findings,
                )
                total_warnings += warnings
                total_errors += errors
                if findings:
                    all_findings = findings
                    self._write_report(project_root, all_findings, check_count)
                    # Only emit to TUI if findings changed since last emission.
                    if findings != last_emitted_findings:
                        self._emit_findings_block(findings, check_count)
                        last_emitted_findings = [dict(f) for f in findings]
                if warnings == 0 and errors == 0 and check_count % self.OK_EMIT_INTERVAL == 0:
                    emit(M.WDOK, f"Check #{check_count} \u2014 all clear ({elapsed:.0f}s elapsed)")
                    # Clear the report file when everything is clean.
                    all_findings = []
                    self._write_report(project_root, all_findings, check_count)
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

    # ── Report persistence ────────────────────────────────────────────

    @staticmethod
    def _write_report(
        project_root: str,
        findings: list[dict[str, str]],
        check_count: int,
    ) -> None:
        """Write structured findings to ``.trust5/watchdog_report.json``."""
        trust5_dir = os.path.join(project_root, ".trust5")
        os.makedirs(trust5_dir, exist_ok=True)
        report_path = os.path.join(trust5_dir, "watchdog_report.json")
        report = {
            "check_number": check_count,
            "findings": findings,
        }
        try:
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
        except OSError:
            logger.debug("Failed to write watchdog report to %s", report_path)

    @staticmethod
    def _emit_findings_block(
        findings: list[dict[str, str]],
        check_count: int,
    ) -> None:
        """Emit findings as a block event so the TUI renders them in a panel."""
        severity_icon = {"error": "\u274c", "warning": "\u26a0\ufe0f"}
        lines: list[str] = []
        for f in findings:
            icon = severity_icon.get(f.get("severity", "warning"), "\u26a0\ufe0f")
            lines.append(f"{icon}  [{f.get('severity', 'warning').upper()}] {f.get('category', '')}")
            lines.append(f"   {f.get('file', '')} \u2014 {f.get('message', '')}")
            lines.append("")
        content = "\n".join(lines).rstrip()
        has_errors = any(f.get("severity") == "error" for f in findings)
        code = M.WDER if has_errors else M.WDWN
        emit_block(code, f"Watchdog Audit (check #{check_count})", content)

    # ── Sentinel helpers ────────────────────────────────────────────

    @staticmethod
    def _pipeline_done(project_root: str) -> bool:
        """Check if the pipeline-complete sentinel exists."""
        return os.path.exists(os.path.join(project_root, ".trust5", _SENTINEL_NAME))

    @staticmethod
    def _clear_sentinel(project_root: str) -> None:
        """Remove the pipeline-complete sentinel file."""
        sentinel = os.path.join(project_root, ".trust5", _SENTINEL_NAME)
        try:
            os.remove(sentinel)
        except FileNotFoundError:
            pass

    # ── Individual checks ─────────────────────────────────────────────

    def _run_checks(
        self,
        project_root: str,
        profile: dict[str, Any],
        context: dict[str, Any],
        findings: list[dict[str, str]],
    ) -> tuple[int, int]:
        """Run all monitoring checks. Returns (warnings, errors)."""
        warnings = 0
        errors = 0

        # File system checks
        w, e = self._check_garbled_files(project_root, findings)
        warnings += w
        errors += e

        w, e = self._check_manifest_files(project_root, profile, findings)
        warnings += w
        errors += e

        w, e = self._check_corrupted_extensions(project_root, findings)
        warnings += w
        errors += e

        w, e = self._check_empty_source_files(project_root, findings)
        warnings += w
        errors += e

        # Context quality checks
        w, e = self._check_stub_files(project_root, findings)
        warnings += w
        errors += e

        return warnings, errors

    def _check_garbled_files(
        self,
        project_root: str,
        findings: list[dict[str, str]],
    ) -> tuple[int, int]:
        """Check for garbled files (shell redirect artifacts)."""
        errors = 0
        try:
            for entry in os.scandir(project_root):
                if entry.is_file() and _GARBLED_RE.match(entry.name):
                    msg = (
                        f"Garbled file detected: {entry.name} (likely shell redirect artifact \u2014 should be deleted)"
                    )

                    findings.append(
                        {
                            "severity": "error",
                            "category": "garbled_file",
                            "file": entry.name,
                            "message": msg,
                        }
                    )
                    errors += 1
        except OSError:
            pass
        return 0, errors

    def _check_manifest_files(
        self,
        project_root: str,
        profile: dict[str, Any],
        findings: list[dict[str, str]],
    ) -> tuple[int, int]:
        """Check that required project manifest files exist."""
        required = profile.get("required_project_files", ())
        warnings = 0
        for req in required:
            full = os.path.join(project_root, req)
            if not os.path.exists(full):
                msg = f"Required project file missing: {req}"

                findings.append(
                    {
                        "severity": "warning",
                        "category": "missing_manifest",
                        "file": req,
                        "message": msg,
                    }
                )
                warnings += 1
        return warnings, 0

    def _check_corrupted_extensions(
        self,
        project_root: str,
        findings: list[dict[str, str]],
    ) -> tuple[int, int]:
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
                    msg = f"Suspicious double extension: {name}"

                    findings.append(
                        {
                            "severity": "warning",
                            "category": "corrupted_extension",
                            "file": name,
                            "message": msg,
                        }
                    )
                    warnings += 1
        except OSError:
            pass
        return warnings, 0

    def _check_empty_source_files(
        self,
        project_root: str,
        findings: list[dict[str, str]],
    ) -> tuple[int, int]:
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
                            msg = f"Empty source file: {rel}"

                            findings.append(
                                {
                                    "severity": "warning",
                                    "category": "empty_source",
                                    "file": rel,
                                    "message": msg,
                                }
                            )
                            warnings += 1
                    except OSError:
                        pass
        except OSError:
            pass
        return warnings, 0

    def _check_stub_files(
        self,
        project_root: str,
        findings: list[dict[str, str]],
    ) -> tuple[int, int]:
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
                            msg = f"Stub file still present: {rel}"

                            findings.append(
                                {
                                    "severity": "warning",
                                    "category": "stub_file",
                                    "file": rel,
                                    "message": msg,
                                }
                            )
                            warnings += 1
                    except OSError:
                        pass
        except OSError:
            pass
        return warnings, 0


def load_watchdog_findings(project_root: str) -> str:
    """Load the latest watchdog report and format it for LLM context injection.

    Returns an empty string if no report exists or no findings are present.
    Called by ``agent_task.py`` and ``repair_task.py`` when building system prompts.
    """
    report_path = os.path.join(project_root, ".trust5", "watchdog_report.json")
    if not os.path.exists(report_path):
        return ""
    try:
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, json.JSONDecodeError):
        return ""

    findings = report.get("findings", [])
    if not findings:
        return ""

    lines = ["## Watchdog Audit Findings (auto-injected)", ""]
    lines.append("The Trust5 Watchdog has detected the following issues in the project.")
    lines.append("You MUST address these if they relate to files you are modifying.\n")

    for finding in findings:
        severity = finding.get("severity", "warning").upper()
        category = finding.get("category", "unknown")
        file_name = finding.get("file", "")
        message = finding.get("message", "")
        lines.append(f"- **[{severity}]** ({category}) `{file_name}`: {message}")

    return "\n".join(lines)
