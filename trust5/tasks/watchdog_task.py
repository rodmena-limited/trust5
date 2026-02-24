"""Trust5 Watchdog — LLM-driven pipeline monitoring system.

Architecture:
  **Layer 1 (Behavioral Rules)**: Language-agnostic pipeline behavior checks
    (repair loops, stalls, regressions, idle agents, jump exhaustion).
  **Layer 2 (LLM Narrator)**: Every-cycle LLM narrative summary that receives
    pipeline state + filesystem listing and decides what is concerning.
  **Layer 3 (Feedback)**: TUI events + atomic report writes + ``load_watchdog_findings()``.

All project content assessment (file quality, manifest validity, stub detection)
is delegated to the LLM — no hardcoded language-specific rules.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from stabilize import StageExecution, Task, TaskResult

from ..core.message import M, emit, emit_block

logger = logging.getLogger(__name__)

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

# Read-only tool codes (agent may be stuck if only these fire)
_READONLY_TOOL_CODES = frozenset({"TRED", "TGLB", "TGRP"})
# Write tool codes (reset idle counter)
_WRITE_TOOL_CODES = frozenset({"TWRT", "TEDT", "TBSH"})

_SENTINEL_NAME = "pipeline_complete"


def signal_pipeline_done(project_root: str) -> None:
    """Write a sentinel file so the watchdog can terminate promptly."""
    sentinel_dir = os.path.join(project_root, ".trust5")
    os.makedirs(sentinel_dir, exist_ok=True)
    sentinel = os.path.join(sentinel_dir, _SENTINEL_NAME)
    try:
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write(str(time.monotonic()))
    except OSError:
        logger.debug("Failed to write pipeline-done sentinel")


_REBUILD_SENTINEL = "watchdog_rebuild"


def signal_rebuild(project_root: str, reason: str) -> None:
    """Write a rebuild sentinel so validate/repair stages trigger reimplementation."""
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
        self.test_pass_history.append(passed)


# ── EventBus consumer ────────────────────────────────────────────────


def _start_event_consumer(health: PipelineHealth) -> tuple[threading.Thread, queue.Queue[Any] | None]:
    """Subscribe to the EventBus and start a daemon thread that updates *health*."""
    from ..core.event_bus import get_bus

    bus = get_bus()
    if bus is None:
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
                break

            code = event.code
            label = event.label

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
            elif code == "VPAS":
                health.record_test_result(True)
                health.last_stage_completion_time = time.monotonic()
            elif code in {"CTLC", "CTLR"}:
                current = health._current_stage or "unknown"
                health.tool_calls_by_stage[current] = health.tool_calls_by_stage.get(current, 0) + 1

            if code in _READONLY_TOOL_CODES:
                health.consecutive_readonly_turns += 1
            elif code in _WRITE_TOOL_CODES:
                health.consecutive_readonly_turns = 0

    thread = threading.Thread(target=_consume, name="watchdog-bus-consumer", daemon=True)
    thread.start()
    return thread, sub_q


# ── Filesystem context for LLM ───────────────────────────────────────


def _collect_filesystem_summary(project_root: str, max_files: int = 100) -> str:
    """Build a concise file listing for the LLM to assess project state.

    Returns a text block listing files with sizes. The LLM uses this to
    identify empty files, stubs, missing manifests, or structural problems
    without any hardcoded language-specific rules.
    """
    entries: list[str] = []
    count = 0
    try:
        for dirpath, dirnames, filenames in os.walk(project_root):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
            for fname in sorted(filenames):
                if count >= max_files:
                    entries.append(f"  ... ({count}+ files, listing truncated)")
                    return "\n".join(entries)
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, project_root)
                try:
                    size = os.path.getsize(full)
                    entries.append(f"  {rel} ({size} bytes)")
                except OSError:
                    entries.append(f"  {rel} (unreadable)")
                count += 1
    except OSError:
        entries.append("  (filesystem walk failed)")
    return "\n".join(entries) if entries else "  (empty project directory)"


# ── LLM Narrator (Layer 2) ─────────────────────────────────────────


def _build_narrative_prompt(
    health: PipelineHealth,
    profile: dict[str, Any],
    behavioral_findings: list[dict[str, str]],
    elapsed_seconds: float,
    check_number: int,
    previous_narrative: str,
    filesystem_summary: str = "",
) -> str:
    """Build a rich prompt for the LLM narrator with full pipeline context."""
    lang = profile.get("language", "unknown")
    elapsed_min = elapsed_seconds / 60

    behavioral_text = (
        "None"
        if not behavioral_findings
        else "\n".join(
            f"  - [{f.get('severity', 'warning').upper()}] {f.get('category', '')}: {f.get('message', '')}"
            for f in behavioral_findings
        )
    )

    test_history = health.test_pass_history
    test_summary = "No test runs yet"
    if test_history:
        passes = sum(1 for t in test_history if t)
        fails = len(test_history) - passes
        recent = test_history[-5:]
        recent_str = " ".join("\u2713" if t else "\u2717" for t in recent)
        test_summary = f"{passes} passed, {fails} failed (recent: {recent_str})"

    return (
        "You are the Trust5 Pipeline Watchdog \u2014 an intelligent observer that monitors\n"
        "a code generation pipeline and provides clear status narratives.\n\n"
        f"Language: {lang}\n"
        f"Elapsed: {elapsed_min:.1f} minutes (check #{check_number})\n\n"
        "PIPELINE BEHAVIOR:\n"
        f"  Stages completed: {', '.join(health.stages_completed) or 'none yet'}\n"
        f"  Stages failed: {', '.join(health.stages_failed) or 'none'}\n"
        f"  Repair attempts: {health.repair_attempts}\n"
        f"  Jump count: {health.jump_count}\n"
        f"  Test results: {test_summary}\n"
        f"  Tool calls by stage: {health.tool_calls_by_stage}\n\n"
        f"BEHAVIORAL ALERTS (pipeline behavior issues):\n{behavioral_text}\n\n"
        f"PROJECT FILES (current state of the project directory):\n"
        f"{filesystem_summary or '  (not available)'}\n\n"
        f"PREVIOUS NARRATIVE (for context only \u2014 do NOT repeat resolved issues):\n"
        f"{previous_narrative or '(first check)'}\n\n"
        "INSTRUCTIONS:\n"
        "Write a 2-4 sentence narrative summary of the pipeline's CURRENT state.\n"
        "- Assess the project files: are there empty files, stubs, missing manifests,\n"
        "  structural issues, or anything that looks wrong for a {lang} project?\n"
        "- Report any behavioral alerts if present\n"
        "- If a previous issue is NO LONGER visible, it was RESOLVED \u2014 do NOT mention it\n"
        "- Tell the user what's happening RIGHT NOW and what to expect next\n"
        "- If things are going well, say so briefly\n"
        "- If the pipeline is stuck or regressing, warn clearly\n"
        "- Do NOT use JSON. Write plain text only.\n"
        "- Be direct and useful, not verbose.\n"
    )


def _run_llm_narrative(
    health: PipelineHealth,
    profile: dict[str, Any],
    behavioral_findings: list[dict[str, str]],
    elapsed_seconds: float,
    check_number: int,
    previous_narrative: str,
    filesystem_summary: str = "",
) -> str | None:
    """Call the LLM for a narrative pipeline summary.  Returns text or ``None`` on failure."""
    try:
        from ..core.llm import LLM

        llm = LLM.for_tier("watchdog", thinking_level=None)
        prompt = _build_narrative_prompt(
            health,
            profile,
            behavioral_findings,
            elapsed_seconds,
            check_number,
            previous_narrative,
            filesystem_summary,
        )
        response = llm.chat(
            messages=[{"role": "user", "content": prompt}],
            timeout=30,
        )
        health.llm_audit_count += 1
        content = response.get("message", {}).get("content", "")
        if isinstance(content, list):
            content = "\n".join(block.get("text", "") for block in content if isinstance(block, dict))
        cleaned = str(content).strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = [ln for ln in lines if not ln.strip().startswith("```")]
            cleaned = "\n".join(lines).strip()
        return cleaned if cleaned else None
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        logger.debug("LLM narrative failed: %s", exc)
        return None


# ── WatchdogTask ─────────────────────────────────────────────


class WatchdogTask(Task):
    """LLM-driven pipeline health monitor.

    Behavioral rules detect pipeline-level issues (loops, stalls, regressions).
    All project content assessment is delegated to the LLM narrator, which
    receives pipeline state and a filesystem listing each cycle.
    """

    CHECK_INTERVAL = 60
    MAX_RUNTIME = 7200
    OK_EMIT_INTERVAL = 5

    def execute(self, stage: StageExecution) -> TaskResult:
        project_root = stage.context.get("project_root", os.getcwd())
        language_profile = stage.context.get("language_profile", {})
        self._clear_sentinel(project_root)

        health = PipelineHealth()

        gcfg = self._load_watchdog_config()
        max_runtime = stage.context.get("workflow_timeout", gcfg.get("max_runtime", self.MAX_RUNTIME))
        check_interval = gcfg.get("check_interval", self.CHECK_INTERVAL)
        ok_emit_interval = gcfg.get("ok_emit_interval", self.OK_EMIT_INTERVAL)
        consumer_thread, sub_q = _start_event_consumer(health)

        emit(M.WDST, "Watchdog started \u2014 monitoring pipeline health")

        start_time = time.monotonic()
        check_count = 0
        previous_narrative: str = ""

        try:
            while True:
                elapsed = time.monotonic() - start_time
                if elapsed > max_runtime:
                    emit(M.WDWN, f"Watchdog max runtime reached ({max_runtime}s). Stopping.")
                    break

                if self._pipeline_done(project_root):
                    emit(
                        M.WDST,
                        f"Pipeline complete \u2014 watchdog shutting down ({elapsed:.0f}s, {check_count} checks)",
                    )
                    self._clear_sentinel(project_root)
                    break

                check_count += 1

                # Layer 1: Language-agnostic behavioral rules
                behavioral_findings: list[dict[str, str]] = []
                self._run_behavioral_rules(health, stage.context, behavioral_findings)
                self._should_trigger_rebuild(health, stage.context, project_root)

                # Filesystem summary for the LLM (no parsing, just listing)
                fs_summary = _collect_filesystem_summary(project_root)

                # Layer 2: LLM Narrative (receives everything, decides what matters)
                narrative = _run_llm_narrative(
                    health,
                    language_profile,
                    behavioral_findings,
                    elapsed,
                    check_count,
                    previous_narrative,
                    fs_summary,
                )
                if narrative:
                    previous_narrative = narrative

                # Layer 3: Feedback
                self._write_report(project_root, behavioral_findings, check_count, health, previous_narrative)

                if narrative:
                    self._emit_narrative_block(narrative, behavioral_findings, check_count)
                elif not behavioral_findings and check_count % ok_emit_interval == 0:
                    emit(M.WDOK, f"Check #{check_count} \u2014 all clear ({elapsed:.0f}s elapsed)")

                if elapsed < 3600:
                    interval = check_interval
                elif elapsed < 21600:
                    interval = 30
                else:
                    interval = 60
                time.sleep(interval)

        except (OSError, RuntimeError) as e:
            emit(M.WDER, f"Watchdog crashed: {e}")
            logger.exception("Watchdog task crashed")

        if sub_q is not None:
            try:
                from ..core.event_bus import get_bus

                bus = get_bus()
                if bus is not None:
                    bus.unsubscribe(sub_q)
            except (OSError, RuntimeError):
                logger.debug("Failed to unsubscribe watchdog from event bus", exc_info=True)

        self._write_report(project_root, [], check_count, health, previous_narrative)

        emit(M.WDST, f"Watchdog stopped after {check_count} checks")
        return TaskResult.success(
            outputs={
                "watchdog_checks": check_count,
            }
        )

    # ── Report persistence (atomic writes) ────────────────────────────

    @staticmethod
    def _write_report(
        project_root: str,
        findings: list[dict[str, str]],
        check_count: int,
        health: PipelineHealth | None = None,
        narrative: str = "",
    ) -> None:
        trust5_dir = os.path.join(project_root, ".trust5")
        os.makedirs(trust5_dir, exist_ok=True)
        report_path = os.path.join(trust5_dir, "watchdog_report.json")
        report: dict[str, Any] = {
            "check_number": check_count,
            "findings": findings,
        }
        if health is not None:
            report["pipeline_health"] = health.to_dict()
        if narrative:
            report["narrative"] = narrative

        try:
            fd, tmp_path = tempfile.mkstemp(dir=trust5_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(report, f, indent=2)
                os.replace(tmp_path, report_path)
            except OSError:
                logger.debug("Failed to write watchdog report, cleaning up temp file", exc_info=True)
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError:
            logger.debug("Failed to write watchdog report to %s", report_path)

    @staticmethod
    def _emit_narrative_block(
        narrative: str,
        behavioral_findings: list[dict[str, str]],
        check_count: int,
    ) -> None:
        lines: list[str] = [narrative]
        if behavioral_findings:
            lines.append("")
            lines.append("\u2500" * 40)
            severity_icon = {"error": "\u274c", "warning": "\u26a0\ufe0f"}
            for f in behavioral_findings:
                icon = severity_icon.get(f.get("severity", "warning"), "\u26a0\ufe0f")
                lines.append(f"{icon}  [{f.get('severity', 'warning').upper()}] {f.get('category', '')}")
                lines.append(f"   {f.get('message', '')}")
                lines.append("")
        content = "\n".join(lines).rstrip()
        if not content:
            return
        has_errors = any(f.get("severity") == "error" for f in behavioral_findings)
        code = M.WDER if has_errors else M.WDWN
        emit_block(code, f"Watchdog (check #{check_count})", content)

    # ── Sentinel helpers ──────────────────────────────────────────────

    @staticmethod
    def _pipeline_done(project_root: str) -> bool:
        return os.path.exists(os.path.join(project_root, ".trust5", _SENTINEL_NAME))

    @staticmethod
    def _clear_sentinel(project_root: str) -> None:
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
            }
        except (OSError, ValueError, KeyError):
            logger.debug("Failed to load watchdog config from GlobalConfig", exc_info=True)
            return {}

    # ── Layer 1: Behavioral rules (language-agnostic) ─────────────────

    def _run_behavioral_rules(
        self,
        health: PipelineHealth,
        context: dict[str, Any],
        findings: list[dict[str, str]],
    ) -> None:
        findings.extend(self._rule_repair_loop(health))
        findings.extend(self._rule_idle_agent(health))
        findings.extend(self._rule_regression(health))
        findings.extend(self._rule_stall(health))
        findings.extend(self._rule_exhaustion(health, context))

    @staticmethod
    def _rule_repair_loop(health: PipelineHealth) -> list[dict[str, str]]:
        findings: list[dict[str, str]] = []
        if health.repair_attempts >= 3:
            findings.append(
                {
                    "severity": "warning",
                    "category": "repair_loop",
                    "file": "",
                    "message": (f"Pipeline has attempted {health.repair_attempts} repairs"
                               " \u2014 may be stuck in repair loop"),
                }
            )
        if health.jump_count >= 20:
            findings.append(
                {
                    "severity": "error",
                    "category": "excessive_jumps",
                    "file": "",
                    "message": f"Pipeline has {health.jump_count} jumps \u2014 likely stuck in infinite loop",
                }
            )
        return findings

    @staticmethod
    def _rule_idle_agent(health: PipelineHealth) -> list[dict[str, str]]:
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
    def _rule_regression(health: PipelineHealth) -> list[dict[str, str]]:
        findings: list[dict[str, str]] = []
        history = health.test_pass_history
        if len(history) < 4:
            return findings
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
        findings: list[dict[str, str]] = []
        if health.last_stage_completion_time <= 0:
            return findings
        stall_duration = time.monotonic() - health.last_stage_completion_time
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
        """Trigger a full rebuild when jump count >= 80% AND no recent progress."""
        max_jumps = context.get("_max_jumps", 50)
        if max_jumps <= 0 or health.jump_count < max_jumps * 0.8:
            return False

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


# ── Public API: load findings for LLM context injection ──────────────


def load_watchdog_findings(project_root: str) -> str:
    """Load the latest watchdog report and format it for LLM context injection.

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
    narrative = report.get("narrative", "")

    if not findings and not narrative:
        return ""

    lines = ["## Watchdog Pipeline Status (auto-injected)", ""]

    if narrative:
        lines.append("### Current Pipeline Assessment")
        lines.append(narrative)
        lines.append("")
    if findings:
        lines.append("### Detected Issues")
        lines.append("You MUST address these if they relate to files you are modifying.\n")
        for finding in findings:
            severity = finding.get("severity", "warning").upper()
            category = finding.get("category", "unknown")
            message = finding.get("message", "")
            lines.append(f"- **[{severity}]** ({category}): {message}")
    return "\n".join(lines)
