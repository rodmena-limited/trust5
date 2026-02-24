"""Trust5 Watchdog — hybrid intelligent pipeline audit system.

Three-layer architecture:
  **Layer 1 (Rule Engine)**: EventBus subscription + deterministic rules (always-on, continuous).
  **Layer 2 (LLM Auditor)**: Checkpoint-triggered LLM analysis (max 3 calls per pipeline).
  **Layer 3 (Feedback)**: TUI events + atomic report writes + enhanced ``load_watchdog_findings()``.

Runs periodic health checks and writes a structured report to
``.trust5/watchdog_report.json`` so downstream LLM agents can
incorporate audit findings into their prompts.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from stabilize import StageExecution, Task, TaskResult

from ..core.message import M, emit, emit_block

logger = logging.getLogger(__name__)

# ── Filesystem check patterns ────────────────────────────────────────

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

# ── Event codes for pipeline health tracking ─────────────────────────

# Read-only tool codes (agent may be stuck if only these fire)
_READONLY_TOOL_CODES = frozenset({"TRED", "TGLB", "TGRP"})
# Write tool codes (reset idle counter)
_WRITE_TOOL_CODES = frozenset({"TWRT", "TEDT", "TBSH"})

# ── Sentinel ─────────────────────────────────────────────────────────

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


# ── Rebuild sentinel ─────────────────────────────────────────────────

_REBUILD_SENTINEL = "watchdog_rebuild"


def signal_rebuild(project_root: str, reason: str) -> None:
    """Write a rebuild sentinel so validate/repair stages trigger reimplementation.

    The watchdog writes this when it determines the project is in an
    unrecoverable state.  ``validate_task`` and ``repair_task`` check for it.
    """
    sentinel_dir = os.path.join(project_root, ".trust5")
    os.makedirs(sentinel_dir, exist_ok=True)
    sentinel = os.path.join(sentinel_dir, _REBUILD_SENTINEL)
    try:
        with open(sentinel, "w", encoding="utf-8") as f:
            json.dump({"reason": reason, "timestamp": time.time()}, f)
        emit(M.WDWN, f"Watchdog signaled rebuild: {reason}")
    except OSError:
        logger.debug("Failed to write rebuild sentinel")


def check_rebuild_signal(project_root: str) -> tuple[bool, str]:
    """Check if the watchdog has signaled a rebuild.  Returns (signaled, reason)."""
    sentinel = os.path.join(project_root, ".trust5", _REBUILD_SENTINEL)
    if not os.path.exists(sentinel):
        return False, ""
    try:
        with open(sentinel, encoding="utf-8") as f:
            data = json.load(f)
        return True, data.get("reason", "watchdog-triggered rebuild")
    except (OSError, json.JSONDecodeError):
        return True, "watchdog-triggered rebuild (unreadable sentinel)"


def clear_rebuild_signal(project_root: str) -> None:
    """Remove the rebuild sentinel after it has been acted on."""
    sentinel = os.path.join(project_root, ".trust5", _REBUILD_SENTINEL)
    try:
        os.remove(sentinel)
    except FileNotFoundError:
        pass


# ── Pipeline Health state machine ────────────────────────────────────


@dataclass
class PipelineHealth:
    """In-memory state machine tracking pipeline behaviour from EventBus events."""

    repair_attempts: int = 0
    jump_count: int = 0
    stages_completed: list[str] = field(default_factory=list)
    stages_failed: list[str] = field(default_factory=list)
    tool_calls_by_stage: dict[str, int] = field(default_factory=dict)
    consecutive_readonly_turns: int = 0
    llm_audit_count: int = 0
    _current_stage: str = ""
    test_pass_history: list[bool] = field(default_factory=list)
    last_stage_completion_time: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "repair_attempts": self.repair_attempts,
            "jump_count": self.jump_count,
            "stages_completed": list(self.stages_completed),
            "stages_failed": list(self.stages_failed),
            "tool_calls_by_stage": dict(self.tool_calls_by_stage),
            "consecutive_readonly_turns": self.consecutive_readonly_turns,
            "test_pass_history": list(self.test_pass_history),
            "last_stage_completion_time": self.last_stage_completion_time,
        }

    def record_test_result(self, passed: bool) -> None:
        """Track test pass/fail history for regression detection."""
        self.test_pass_history.append(passed)


# ── EventBus consumer ────────────────────────────────────────────────


def _start_event_consumer(health: PipelineHealth) -> tuple[threading.Thread, queue.Queue[Any] | None]:
    """Subscribe to the EventBus and start a daemon thread that updates *health*.

    Returns ``(thread, subscriber_queue)``.  If the bus is not initialized
    the thread is a no-op and the queue is ``None``.
    """
    from ..core.event_bus import get_bus

    bus = get_bus()
    if bus is None:
        # No EventBus available — return a no-op thread.
        dummy: threading.Thread = threading.Thread(target=lambda: None, name="watchdog-bus-noop", daemon=True)
        dummy.start()
        return dummy, None

    sub_q = bus.subscribe()

    def _consume() -> None:
        while True:
            try:
                event = sub_q.get(timeout=1.0)
            except queue.Empty:
                continue
            if event is None:
                break  # sentinel — bus shutting down

            code = event.code
            label = event.label

            # Stage transitions
            if code == "WJMP":
                health.jump_count += 1
            elif code == "RSTR":
                health.repair_attempts += 1
            elif code == "WSTG":
                stage_name = label or event.msg
                if stage_name:
                    stage_name = stage_name.strip().lower()
                    if stage_name and stage_name not in health.stages_completed:
                        health.stages_completed.append(stage_name)
                    health._current_stage = stage_name
                    health.last_stage_completion_time = time.monotonic()
            elif code in {"VFAL", "WFAL", "QFAL"}:
                stage_name = label or event.msg or ""
                stage_name = stage_name.strip().lower()
                if stage_name and stage_name not in health.stages_failed:
                    health.stages_failed.append(stage_name)
                if code == "VFAL":
                    health.record_test_result(False)

            # Test result tracking for regression detection
            elif code == "VPAS":
                health.record_test_result(True)
                health.last_stage_completion_time = time.monotonic()

            # Tool call tracking
            elif code in {"CTLC", "CTLR"}:
                current = health._current_stage or "unknown"
                health.tool_calls_by_stage[current] = health.tool_calls_by_stage.get(current, 0) + 1

            # Read-only vs write tool tracking (idle detection)
            if code in _READONLY_TOOL_CODES:
                health.consecutive_readonly_turns += 1
            elif code in _WRITE_TOOL_CODES:
                health.consecutive_readonly_turns = 0

    thread = threading.Thread(target=_consume, name="watchdog-bus-consumer", daemon=True)
    thread.start()
    return thread, sub_q


# ── LLM Narrator (Layer 2) ─────────────────────────────────────────


def _build_narrative_prompt(
    health: PipelineHealth,
    profile: dict[str, Any],
    rule_findings: list[dict[str, str]],
    elapsed_seconds: float,
    check_number: int,
    previous_narrative: str,
) -> str:
    """Build a rich prompt for the LLM narrator with full pipeline context."""
    lang = profile.get('language', 'unknown')
    elapsed_min = elapsed_seconds / 60

    findings_text = "None" if not rule_findings else "\n".join(
        f"  - [{f.get('severity', 'warning').upper()}] {f.get('category', '')}: "
        f"{f.get('message', '')}"
        for f in rule_findings
    )

    test_history = health.test_pass_history
    test_summary = "No test runs yet"
    if test_history:
        passes = sum(1 for t in test_history if t)
        fails = len(test_history) - passes
        recent = test_history[-5:]
        recent_str = " ".join("✓" if t else "✗" for t in recent)
        test_summary = f"{passes} passed, {fails} failed (recent: {recent_str})"
    return (
        "You are the Trust5 Pipeline Watchdog — an intelligent observer that provides\n"
        "clear, concise status narratives for the user watching a code generation pipeline.\n\n"
        f"Language: {lang}\n"
        f"Elapsed: {elapsed_min:.1f} minutes (check #{check_number})\n\n"
        "PIPELINE STATE:\n"
        f"  Stages completed: {', '.join(health.stages_completed) or 'none yet'}\n"
        f"  Stages failed: {', '.join(health.stages_failed) or 'none'}\n"
        f"  Repair attempts: {health.repair_attempts}\n"
        f"  Jump count: {health.jump_count}\n"
        f"  Test results: {test_summary}\n"
        f"  Tool calls by stage: {health.tool_calls_by_stage}\n\n"
        f"AUTOMATED CHECK FINDINGS:\n{findings_text}\n\n"
        f"PREVIOUS NARRATIVE:\n{previous_narrative or '(first check)'}\n\n"
        "INSTRUCTIONS:\n"
        "Write a 2-4 sentence narrative summary of the pipeline's current state.\n"
        "- Tell the user what's happening RIGHT NOW and what to expect next\n"
        "- If there are problems, explain what they mean in plain language\n"
        "- If things are going well, say so briefly\n"
        "- If the pipeline is stuck or regressing, warn clearly\n"
        "- Include specific file names or error types when relevant\n"
        "- Do NOT use JSON. Write plain text only.\n"
        "- Do NOT repeat the raw findings — interpret them for the user.\n"
        "- Be direct and useful, not verbose.\n"
    )


def _run_llm_narrative(
    health: PipelineHealth,
    profile: dict[str, Any],
    rule_findings: list[dict[str, str]],
    elapsed_seconds: float,
    check_number: int,
    previous_narrative: str,
) -> str | None:
    """Call the LLM for a narrative pipeline summary.  Returns text or ``None`` on failure."""
    try:
        from ..core.llm import LLM
        prompt = _build_narrative_prompt(
            health, profile, rule_findings, elapsed_seconds, check_number, previous_narrative,
        )
        response = llm.chat(messages=[{"role": "user", "content": prompt}])
        health.llm_audit_count += 1
        # Strip any markdown fences the LLM might add
        cleaned = content.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = [ln for ln in lines if not ln.strip().startswith("```")]
            cleaned = "\n".join(lines).strip()

        return cleaned if cleaned else None
    except Exception as exc:
        logger.debug("LLM narrative failed: %s", exc)
        return None


# ── WatchdogTask ─────────────────────────────────────────────


class WatchdogTask(Task):
    """Autonomous pipeline health monitor with hybrid rule + LLM audit system.

    Layers:
      1. **Rule Engine** — Deterministic checks on filesystem and ``PipelineHealth``.
      2. **LLM Auditor** — Checkpoint-triggered analysis (≤3 calls per pipeline).
      3. **Feedback** — TUI events, atomic report writes, ``load_watchdog_findings()``.
    """

    CHECK_INTERVAL = 12
    MAX_RUNTIME = 7200
    OK_EMIT_INTERVAL = 25
    def execute(self, stage: StageExecution) -> TaskResult:
        project_root = stage.context.get("project_root", os.getcwd())
        language_profile = stage.context.get("language_profile", {})
        self._clear_sentinel(project_root)

        health = PipelineHealth()

        gcfg = self._load_watchdog_config()
        max_runtime = stage.context.get("workflow_timeout", gcfg.get("max_runtime", self.MAX_RUNTIME))
        check_interval = gcfg.get("check_interval", self.CHECK_INTERVAL)
        ok_emit_interval = gcfg.get("ok_emit_interval", self.OK_EMIT_INTERVAL)
        max_llm_audits = gcfg.get("max_llm_audits", _MAX_LLM_AUDITS)
        consumer_thread, sub_q = _start_event_consumer(health)

        emit(M.WDST, "Watchdog started \u2014 monitoring pipeline health")

        start_time = time.monotonic()
        check_count = 0
        total_warnings = 0
        total_errors = 0
        all_findings: list[dict[str, str]] = []
        last_emitted_findings: list[dict[str, str]] = []
        audit_summaries: list[dict[str, Any]] = []
        audit_triggers_fired: set[str] = set()

        try:
            while True:
                elapsed = time.monotonic() - start_time
                if elapsed > max_runtime:
                    emit(M.WDWN, f"Watchdog max runtime reached ({max_runtime}s). Stopping.")
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

                # ── Layer 1a: Deterministic rules ────────────────────
                self._run_rules(project_root, language_profile, stage.context, health, findings)

                # Check for rebuild trigger
                self._should_trigger_rebuild(health, stage.context, project_root)
                # ── Layer 1b: Filesystem checks (existing) ──────────
                warnings, errors = self._run_checks(project_root, language_profile, stage.context, findings)
                total_warnings += warnings
                total_errors += errors

                # ── Layer 2: LLM audit triggers ─────────────────────
                max_audits = max(max_llm_audits, int(elapsed / 3600 / 2))  # ~1 audit per 2 hours
                trigger = _should_trigger_audit(health, findings, audit_triggers_fired, max_audits)
                if trigger is not None:
                    audit_result = _run_llm_audit(health, language_profile, findings, trigger, max_audits)
                    if audit_result is not None:
                        audit_summaries.append(audit_result)
                        audit_triggers_fired.add(trigger)

                # ── Layer 3: Feedback ───────────────────────────────
                if findings:
                    all_findings = findings
                    self._write_report(project_root, all_findings, check_count, health, audit_summaries)
                    if findings != last_emitted_findings:
                        self._emit_findings_block(findings, check_count)
                        last_emitted_findings = [dict(f) for f in findings]

                if warnings == 0 and errors == 0 and check_count % ok_emit_interval == 0:
                    emit(M.WDOK, f"Check #{check_count} \u2014 all clear ({elapsed:.0f}s elapsed)")
                    all_findings = []
                    self._write_report(project_root, all_findings, check_count, health, audit_summaries)

                # Progressive interval: tighter at start, relaxed for long runs
                if elapsed < 3600:
                    interval = check_interval
                elif elapsed < 21600:
                    interval = 30  # 6 hours
                else:
                    interval = 60  # beyond 6 hours
                time.sleep(interval)

        except Exception as e:
            emit(M.WDER, f"Watchdog crashed: {e}")
            logger.exception("Watchdog task crashed")

        # ── Cleanup ─────────────────────────────────────────────────
        # Unsubscribe from EventBus
        if sub_q is not None:
            try:
                from ..core.event_bus import get_bus

                bus = get_bus()
                if bus is not None:
                    bus.unsubscribe(sub_q)
            except Exception:
                logger.debug("Failed to unsubscribe watchdog from event bus", exc_info=True)

        # Final report write
        self._write_report(project_root, all_findings, check_count, health, audit_summaries)

        emit(M.WDST, f"Watchdog stopped after {check_count} checks ({total_warnings} warnings, {total_errors} errors)")
        return TaskResult.success(
            outputs={
                "watchdog_checks": check_count,
                "watchdog_warnings": total_warnings,
                "watchdog_errors": total_errors,
            }
        )

    # ── Report persistence (atomic writes) ────────────────────────────

    @staticmethod
    def _write_report(
        project_root: str,
        findings: list[dict[str, str]],
        check_count: int,
        health: PipelineHealth | None = None,
        audit_summaries: list[dict[str, Any]] | None = None,
    ) -> None:
        """Write structured findings to ``.trust5/watchdog_report.json`` atomically."""
        trust5_dir = os.path.join(project_root, ".trust5")
        os.makedirs(trust5_dir, exist_ok=True)
        report_path = os.path.join(trust5_dir, "watchdog_report.json")
        report: dict[str, Any] = {
            "check_number": check_count,
            "findings": findings,
        }
        if health is not None:
            report["pipeline_health"] = health.to_dict()
        if audit_summaries:
            report["audit_summaries"] = audit_summaries

        # Atomic write: write to temp file then rename
        try:
            fd, tmp_path = tempfile.mkstemp(dir=trust5_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(report, f, indent=2)
                os.replace(tmp_path, report_path)
            except Exception:
                logger.debug("Failed to write watchdog report, cleaning up temp file", exc_info=True)
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
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

    # ── Sentinel helpers ──────────────────────────────────────────────

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

    @staticmethod
    def _load_watchdog_config() -> dict[str, int]:
        try:
            from ..core.config import load_global_config
            cfg = load_global_config().watchdog
            return {
                "max_runtime": cfg.max_runtime,
                "check_interval": cfg.check_interval,
                "ok_emit_interval": cfg.ok_emit_interval,
                "max_llm_audits": cfg.max_llm_audits,
            }
        except Exception:
            logger.debug("Failed to load watchdog config from GlobalConfig", exc_info=True)
            return {}

    # ── Layer 1a: Deterministic rules ─────────────────────────────────

    def _run_rules(
        self,
        project_root: str,
        profile: dict[str, Any],
        context: dict[str, Any],
        health: PipelineHealth,
        findings: list[dict[str, str]],
    ) -> None:
        """Execute all deterministic rules and append findings."""
        findings.extend(self._rule_tool_availability(profile))
        findings.extend(self._rule_test_discovery(project_root, profile, health))
        findings.extend(self._rule_manifest_valid(project_root, profile))
        findings.extend(self._rule_repair_loop(health))
        findings.extend(self._rule_idle_agent(health))
        findings.extend(self._rule_quality_prerequisites(project_root, profile, health))
        findings.extend(self._rule_cross_module_consistency(project_root, context))
        findings.extend(self._rule_regression(health))
        findings.extend(self._rule_stall(health))
        findings.extend(self._rule_exhaustion(health, context))

    @staticmethod
    def _rule_tool_availability(profile: dict[str, Any]) -> list[dict[str, str]]:
        """Rule 1: Verify required tool binaries exist on PATH."""
        findings: list[dict[str, str]] = []
        for cmd in profile.get("tool_check_commands", ()):
            binary = cmd.split()[0] if cmd else ""
            if binary and shutil.which(binary) is None:
                findings.append(
                    {
                        "severity": "warning",
                        "category": "tool_missing",
                        "file": binary,
                        "message": f"Required tool not found on PATH: {binary} (from command: {cmd})",
                    }
                )
        return findings

    @staticmethod
    def _rule_test_discovery(
        project_root: str,
        profile: dict[str, Any],
        health: PipelineHealth,
    ) -> list[dict[str, str]]:
        """Rule 2: After implement stage, check that test files exist."""
        findings: list[dict[str, str]] = []
        if "implement" not in health.stages_completed:
            return findings
        if not profile.get("test_discovery_command"):
            return findings

        extensions = set(profile.get("extensions", ()))
        found_test = False
        try:
            for dirpath, dirnames, filenames in os.walk(project_root):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
                for fname in filenames:
                    _, ext = os.path.splitext(fname)
                    if ext.lower() not in extensions:
                        continue
                    lower = fname.lower()
                    if lower.startswith("test_") or "_test" in lower:
                        found_test = True
                        break
                if found_test:
                    break
        except OSError:
            pass

        if not found_test:
            findings.append(
                {
                    "severity": "warning",
                    "category": "no_tests",
                    "file": "",
                    "message": "No test files found after implement stage — test discovery may fail",
                }
            )
        return findings

    @staticmethod
    def _rule_manifest_valid(
        project_root: str,
        profile: dict[str, Any],
    ) -> list[dict[str, str]]:
        """Rule 3: Run manifest validators and report failures."""
        findings: list[dict[str, str]] = []
        for cmd in profile.get("manifest_validators", ()):
            try:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    timeout=10,
                    cwd=project_root,
                )
                if result.returncode != 0:
                    stderr = result.stderr.decode("utf-8", errors="replace")[:200]
                    findings.append(
                        {
                            "severity": "error",
                            "category": "manifest_invalid",
                            "file": cmd,
                            "message": f"Manifest validation failed: {cmd} (exit {result.returncode}): {stderr}",
                        }
                    )
            except (subprocess.TimeoutExpired, OSError) as exc:
                findings.append(
                    {
                        "severity": "warning",
                        "category": "manifest_check_failed",
                        "file": cmd,
                        "message": f"Manifest validation could not run: {cmd} ({exc})",
                    }
                )
        return findings

    @staticmethod
    def _rule_repair_loop(health: PipelineHealth) -> list[dict[str, str]]:
        """Rule 4: Detect excessive repair looping."""
        findings: list[dict[str, str]] = []
        if health.repair_attempts >= 3:
            findings.append(
                {
                    "severity": "warning",
                    "category": "repair_loop",
                    "file": "",
                    "message": f"Pipeline has attempted {health.repair_attempts} repairs — may be stuck in repair loop",
                }
            )
        if health.jump_count >= 20:
            findings.append(
                {
                    "severity": "error",
                    "category": "excessive_jumps",
                    "file": "",
                    "message": f"Pipeline has {health.jump_count} jumps — likely stuck in infinite loop",
                }
            )
        return findings

    @staticmethod
    def _rule_idle_agent(health: PipelineHealth) -> list[dict[str, str]]:
        """Rule 5: Detect agent stuck in read-only loop."""
        findings: list[dict[str, str]] = []
        if health.consecutive_readonly_turns >= 8:
            findings.append(
                {
                    "severity": "warning",
                    "category": "idle_agent",
                    "file": "",
                    "message": (
                        f"Agent appears stuck in read-only loop "
                        f"({health.consecutive_readonly_turns} consecutive read-only tool calls)"
                    ),
                }
            )
        return findings

    @staticmethod
    def _rule_quality_prerequisites(
        project_root: str,
        profile: dict[str, Any],
        health: PipelineHealth,
    ) -> list[dict[str, str]]:
        """Rule 6: Before quality gate, verify required project files exist."""
        findings: list[dict[str, str]] = []
        if "quality" in health.stages_completed:
            return findings  # Already past quality — no point checking
        for req in profile.get("required_project_files", ()):
            full = os.path.join(project_root, req)
            if not os.path.exists(full):
                findings.append(
                    {
                        "severity": "warning",
                        "category": "quality_prereq_missing",
                        "file": req,
                        "message": f"Required project file missing before quality gate: {req}",
                    }
                )
        return findings

    @staticmethod
    def _rule_cross_module_consistency(
        project_root: str,
        context: dict[str, Any],
    ) -> list[dict[str, str]]:
        """Rule 7: Verify owned_files from context actually exist on disk."""
        findings: list[dict[str, str]] = []
        owned = context.get("owned_files")
        if not owned or not isinstance(owned, (list, tuple, set)):
            return findings
        for fpath in owned:
            full = os.path.join(project_root, fpath) if not os.path.isabs(fpath) else fpath
            if not os.path.exists(full):
                findings.append(
                    {
                        "severity": "warning",
                        "category": "owned_file_missing",
                        "file": fpath,
                        "message": f"Owned file does not exist on disk: {fpath}",
                    }
                )
        return findings

    @staticmethod
    def _rule_regression(health: PipelineHealth) -> list[dict[str, str]]:
        """Rule 8: Detect declining test pass rate (regression)."""
        findings: list[dict[str, str]] = []
        history = health.test_pass_history
        if len(history) < 4:
            return findings
        # Check if last 3 results are all failures after at least 1 pass
        if any(history[:-3]) and all(not r for r in history[-3:]):
            findings.append(
                {
                    "severity": "error",
                    "category": "regression",
                    "file": "",
                    "message": (
                        f"Test regression detected: last 3 test runs failed after previous passes "
                        f"(history: {len([r for r in history if r])}/{len(history)} passes)"
                    ),
                }
            )
        return findings

    @staticmethod
    def _rule_stall(health: PipelineHealth) -> list[dict[str, str]]:
        """Rule 9: Detect pipeline stall (no stage completions for extended period)."""
        findings: list[dict[str, str]] = []
        if health.last_stage_completion_time <= 0:
            return findings
        stall_duration = time.monotonic() - health.last_stage_completion_time
        # After 30 minutes of no stage completions, warn
        if stall_duration > 1800:
            findings.append(
                {
                    "severity": "warning" if stall_duration < 3600 else "error",
                    "category": "pipeline_stall",
                    "file": "",
                    "message": (
                        f"Pipeline stall: no stage completed in {stall_duration / 60:.0f} minutes. "
                        f"Last stages: {health.stages_completed[-3:] if health.stages_completed else 'none'}"
                    ),
                }
            )
        return findings

    @staticmethod
    def _rule_exhaustion(health: PipelineHealth, context: dict[str, Any]) -> list[dict[str, str]]:
        """Rule 10: Detect when jump count is approaching the limit."""
        findings: list[dict[str, str]] = []
        max_jumps = context.get("_max_jumps", 50)
        if max_jumps <= 0:
            return findings
        ratio = health.jump_count / max_jumps
        if ratio >= 0.8:
            findings.append(
                {
                    "severity": "error",
                    "category": "jump_exhaustion",
                    "file": "",
                    "message": (
                        f"Jump limit nearly exhausted: {health.jump_count}/{max_jumps} "
                        f"({ratio:.0%}). Pipeline may terminate soon."
                    ),
                }
            )
        elif ratio >= 0.6:
            findings.append(
                {
                    "severity": "warning",
                    "category": "jump_exhaustion",
                    "file": "",
                    "message": f"Jump count at {health.jump_count}/{max_jumps} ({ratio:.0%}).",
                }
            )
        return findings

    def _should_trigger_rebuild(
        self,
        health: PipelineHealth,
        context: dict[str, Any],
        project_root: str,
    ) -> bool:
        """Determine if a full project rebuild should be triggered.

        Fires when jump count >= 80% of limit AND no recent progress
        (test regression or stall).
        """
        max_jumps = context.get("_max_jumps", 50)
        if max_jumps <= 0 or health.jump_count < max_jumps * 0.8:
            return False

        # Need evidence of being stuck, not just high jump count
        history = health.test_pass_history
        recent_regression = len(history) >= 3 and all(not r for r in history[-3:])

        stall = False
        if health.last_stage_completion_time > 0:
            stall = (time.monotonic() - health.last_stage_completion_time) > 1800

        if recent_regression or stall:
            signal_rebuild(
                project_root,
                f"Jump count {health.jump_count}/{max_jumps} with "
                f"{'test regression' if recent_regression else 'pipeline stall'}",
            )
            return True
        return False

    # ── Layer 1b: Filesystem checks (preserved from original) ─────────

    def _run_checks(
        self,
        project_root: str,
        profile: dict[str, Any],
        context: dict[str, Any],
        findings: list[dict[str, str]],
    ) -> tuple[int, int]:
        """Run all filesystem monitoring checks. Returns (warnings, errors)."""
        warnings = 0
        errors = 0

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

        w, e = self._check_stub_files(project_root, findings)
        warnings += w
        errors += e

        return warnings, errors

    def _check_garbled_files(
        self,
        project_root: str,
        findings: list[dict[str, str]],
    ) -> tuple[int, int]:
        """Check for and auto-delete garbled files (shell redirect artifacts)."""
        errors = 0
        try:
            for entry in os.scandir(project_root):
                if entry.is_file() and _GARBLED_RE.match(entry.name):
                    try:
                        os.remove(entry.path)
                        msg = f"Garbled file auto-deleted: {entry.name} (shell redirect artifact)"
                        emit(M.WDWN, f"Auto-deleted garbled file: {entry.name}")
                    except OSError:
                        msg = f"Garbled file detected but could not delete: {entry.name}"
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


# ── Public API: load findings for LLM context injection ──────────────


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

    # Include LLM audit summaries if present
    audit_summaries = report.get("audit_summaries", [])
    if audit_summaries:
        lines.append("\n## LLM Audit Summaries\n")
        for summary in audit_summaries:
            lines.append(f"**Risk: {summary.get('risk', 'UNKNOWN')}** (trigger: {summary.get('trigger', '')})")
            for concern in summary.get("concerns", []):
                lines.append(f"- \u26a0\ufe0f {concern}")
            for rec in summary.get("recommendations", []):
                lines.append(f"- \U0001f4a1 {rec}")
            lines.append("")

    return "\n".join(lines)
