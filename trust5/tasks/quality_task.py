"""TRUST 5 quality gate task — runs validators and jumps to repair on failure."""

import logging
import os
from typing import Any

from stabilize import StageExecution, Task, TaskResult

from ..core.config import ConfigManager, QualityConfig
from ..core.constants import QUALITY_OUTPUT_LIMIT
from ..core.context_keys import increment_jump_count, propagate_context
from ..core.lang import LanguageProfile
from ..core.message import M, emit, emit_block
from ..core.quality import (
    QualityReport,
    TrustGate,
    is_stagnant,
    meets_quality_gate,
)
from ..core.quality_gates import (
    Assessment,
    DiagnosticSnapshot,
    MethodologyContext,
    build_snapshot_from_report,
    detect_regression,
    validate_methodology,
    validate_phase,
)
from ..tasks.watchdog_task import signal_pipeline_done

logger = logging.getLogger(__name__)


class QualityTask(Task):
    """Runs TRUST 5 quality gate; jumps to repair on failure."""

    def execute(self, stage: StageExecution) -> TaskResult:
        project_root = stage.context.get("project_root", os.getcwd())
        attempt = stage.context.get("quality_attempt", 0)
        max_attempts = stage.context.get("max_quality_attempts", 3)
        profile_data = stage.context.get("language_profile", {})
        prev_report = stage.context.get("prev_quality_report")
        tests_partial = stage.context.get("tests_partial", False)

        config = self._load_quality_config(project_root)

        plan_config = stage.context.get("plan_config", {})
        if plan_config:
            if plan_config.get("quality_threshold") is not None:
                config.pass_score_threshold = max(0.1, min(1.0, float(plan_config["quality_threshold"])))
            if plan_config.get("lint_command"):
                config.plan_lint_command = str(plan_config["lint_command"])
            if plan_config.get("test_command"):
                config.plan_test_command = str(plan_config["test_command"])
            if plan_config.get("coverage_command"):
                config.plan_coverage_command = str(plan_config["coverage_command"])

        if not config.enforce_quality:
            emit(M.QRUN, "Quality enforcement disabled — skipping TRUST 5 gate")
            return TaskResult.success(outputs={"quality_passed": True, "quality_skipped": True})

        profile = self._build_profile(profile_data, project_root)

        if tests_partial:
            emit(
                M.QRUN,
                f"TRUST 5 quality gate (attempt {attempt}/{max_attempts}) [{profile.language}] "
                f"[tests_partial — test failures are known, repair exhausted]",
            )
        else:
            emit(
                M.QRUN,
                f"TRUST 5 quality gate (attempt {attempt}/{max_attempts}) [{profile.language}]",
            )

        gate = TrustGate(config=config, profile=profile, project_root=project_root)
        report = gate.validate()

        current_phase = stage.context.get("pipeline_phase", "run")
        snapshot = build_snapshot_from_report(report)
        phase_issues = validate_phase(current_phase, snapshot, config)
        if phase_issues:
            emit(M.QVAL, f"Phase gate ({current_phase}): {len(phase_issues)} issue(s)")
            for pi in phase_issues:
                emit(M.QVAL, f"  [{pi.severity}] {pi.message}")

        baseline_data = stage.context.get("diagnostic_baseline")
        if baseline_data:
            baseline = DiagnosticSnapshot(**baseline_data)
            reg_issues = detect_regression(baseline, snapshot, config)
            if reg_issues:
                emit(M.QVAL, f"Regression detected: {len(reg_issues)} issue(s)")
                for ri in reg_issues:
                    emit(M.QVAL, f"  [{ri.severity}] {ri.message}")
                phase_issues.extend(reg_issues)

        dev_mode = stage.context.get("development_mode", "hybrid")

        # Extract assertion density from the Tested pillar's hint issues
        assertion_density = -1.0
        tested_pr = report.principles.get("tested")
        if tested_pr:
            for issue in tested_pr.issues:
                if issue.rule == "assertion-density-measured":
                    try:
                        assertion_density = float(issue.message.split("=")[1])
                    except (IndexError, ValueError):
                        pass

        # Extract mutation score from upstream mutation stage (if it ran)
        mutation_score = float(stage.context.get("mutation_score", -1.0))

        methodology_ctx = MethodologyContext(
            test_first_verified=bool(stage.context.get("test_first_completed", False)),
            assertion_density=assertion_density,
            mutation_score=mutation_score,
        )
        method_issues = validate_methodology(dev_mode, methodology_ctx, config)
        if method_issues:
            emit(M.QVAL, f"Methodology ({dev_mode}): {len(method_issues)} issue(s)")
            for mi in method_issues:
                emit(M.QVAL, f"  [{mi.severity}] {mi.message}")
            phase_issues.extend(method_issues)

        assessment = Assessment(report)
        self._emit_report(report, assessment)

        # ── SPEC compliance check ──
        compliance_report = None
        acceptance_criteria = plan_config.get("acceptance_criteria", [])
        if acceptance_criteria and config.spec_compliance_enabled:
            from ..core.compliance import check_compliance
            from ..core.llm import LLM

            try:
                compliance_llm = LLM.for_tier("fast", thinking_level=None)
            except Exception:
                compliance_llm = None
            compliance_report = check_compliance(
                acceptance_criteria,
                project_root,
                extensions=profile.extensions,
                skip_dirs=profile.skip_dirs,
                llm=compliance_llm,
            )
            emit(
                M.QRUN,
                f"SPEC compliance: {compliance_report.criteria_met}/{compliance_report.criteria_total} "
                f"criteria met (ratio={compliance_report.compliance_ratio:.2f}, "
                f"threshold={config.spec_compliance_threshold})",
            )
            for cr in compliance_report.results:
                if cr.status != "met":
                    emit(M.QVAL, f"  [{cr.status.upper()}] {cr.criterion}")
                    if cr.searched_identifiers:
                        missing = set(cr.searched_identifiers) - set(cr.matched_identifiers)
                        if missing:
                            emit(M.QVAL, f"    Missing: {', '.join(sorted(missing))}")

        compliance_passed = (
            compliance_report is None or compliance_report.compliance_ratio >= config.spec_compliance_threshold
        )
        compliance_outputs: dict[str, object] = {"compliance_passed": compliance_passed}
        if compliance_report is not None:
            compliance_outputs.update(
                {
                    "spec_compliance_ratio": compliance_report.compliance_ratio,
                    "spec_criteria_met": compliance_report.criteria_met,
                    "spec_criteria_total": compliance_report.criteria_total,
                    "spec_unmet_criteria": list(compliance_report.unmet_criteria),
                }
            )

        has_phase_blockers = any(i.severity == "error" for i in phase_issues)
        if meets_quality_gate(report, config) and not has_phase_blockers and compliance_passed:
            status = assessment.overall_status()
            emit(
                M.QPAS,
                f"Quality gate PASSED — score {report.score:.3f} (threshold {config.pass_score_threshold}) [{status}]",
            )
            signal_pipeline_done(project_root)
            return TaskResult.success(
                outputs={
                    "quality_passed": True,
                    "quality_score": report.score,
                    "quality_errors": report.total_errors,
                    "quality_warnings": report.total_warnings,
                    "quality_attempts_used": attempt,
                    "assessment_status": status,
                    **compliance_outputs,
                }
            )

        # ── When tests_partial=True, repair already exhausted its attempts ──
        # Do NOT enter a quality→repair→validate loop for known test failures.
        # Report results and accept partial.
        if tests_partial:
            emit(
                M.QFAL,
                f"Quality gate FAILED (score {report.score:.3f}) — "
                f"tests_partial=True (repair exhausted). Accepting partial result.",
            )
            signal_pipeline_done(project_root)
            return TaskResult.failed_continue(
                error=(f"Quality gate failed (score={report.score:.3f}) with partial test results (repair exhausted)"),
                outputs={
                    "quality_passed": False,
                    "quality_score": report.score,
                    "quality_errors": report.total_errors,
                    "quality_warnings": report.total_warnings,
                    "quality_attempts_used": attempt,
                    "tests_partial": True,
                    **compliance_outputs,
                },
            )

        # Gate failed — decide whether to repair or accept partial
        if attempt >= max_attempts:
            emit(
                M.QFAL,
                f"Max quality attempts ({max_attempts}) reached. Accepting partial result.",
            )
            signal_pipeline_done(project_root)
            return TaskResult.failed_continue(
                error=f"Quality gate failed after {attempt} attempts (score={report.score:.3f})",
                outputs={
                    "quality_passed": False,
                    "quality_score": report.score,
                    "quality_errors": report.total_errors,
                    "quality_attempts_used": attempt,
                    **compliance_outputs,
                },
            )

        quality_gate_ok = meets_quality_gate(report, config) and not has_phase_blockers
        compliance_is_only_blocker = quality_gate_ok and not compliance_passed
        if is_stagnant(prev_report, report) and not compliance_is_only_blocker:
            emit(
                M.QFAL,
                "Quality stagnant — no improvement between attempts. Accepting partial.",
            )
            signal_pipeline_done(project_root)
            return TaskResult.failed_continue(
                error=f"Quality stagnant at score={report.score:.3f}",
                outputs={
                    "quality_passed": False,
                    "quality_score": report.score,
                    "quality_errors": report.total_errors,
                    "quality_attempts_used": attempt,
                    **compliance_outputs,
                },
            )

        feedback = self._format_quality_feedback(report, config, phase_issues)

        # Append unmet criteria to repair feedback so repairer knows what's missing
        if compliance_report and compliance_report.unmet_criteria:
            unmet_section = "\n\n## SPEC COMPLIANCE — UNMET CRITERIA\n\n"
            unmet_section += "The following acceptance criteria are NOT addressed in the source code:\n"
            for uc in compliance_report.unmet_criteria:
                unmet_section += f"  - {uc}\n"
            unmet_section += (
                "\nThese are MISSING FEATURES, not bugs. "
                "You must ADD the missing functionality (new classes, methods, or modules).\n"
            )
            feedback += unmet_section
        emit(
            M.QJMP,
            f"Quality gate FAILED (score {report.score:.3f}). Jumping to repair.",
        )

        snapshot_data = {
            "errors": snapshot.errors,
            "warnings": snapshot.warnings,
            "type_errors": snapshot.type_errors,
            "lint_errors": snapshot.lint_errors,
            "security_warnings": snapshot.security_warnings,
            "timestamp": snapshot.timestamp,
        }

        jump_repair_ref = stage.context.get("jump_repair_ref", "repair")
        jump_context: dict[str, Any] = {
            "_repair_requested": True,
            "test_output": feedback[:QUALITY_OUTPUT_LIMIT],
            "previous_failures": stage.context.get("previous_failures", []),
            "failure_type": "quality",
            "project_root": project_root,
            "quality_attempt": attempt + 1,
            "max_quality_attempts": max_attempts,
            "prev_quality_report": report.model_dump(),
            "language_profile": profile_data,
            "diagnostic_baseline": baseline_data or snapshot_data,
            "pipeline_phase": current_phase,
        }
        propagate_context(stage.context, jump_context)
        increment_jump_count(jump_context)
        return TaskResult.jump_to(jump_repair_ref, context=jump_context)

    # ── helpers ──

    @staticmethod
    def _load_quality_config(project_root: str) -> QualityConfig:
        try:
            mgr = ConfigManager(project_root)
            cfg = mgr.load_config()
            return cfg.quality
        except (OSError, ValueError, KeyError) as e:  # config loading errors
            logger.warning("Failed to load quality config: %s — using defaults", e)
            return QualityConfig()

    @staticmethod
    def _build_profile(data: dict[str, Any], project_root: str = ".") -> LanguageProfile:
        # Always re-detect language from the project to avoid stale 'unknown' profiles.
        # The initial context may have language='unknown' if detection ran before source
        # files existed.  By the time the quality gate runs, the files ARE there.
        from ..core.lang import detect_language, get_profile
        detected = detect_language(project_root)
        base = get_profile(detected)

        if not data or data.get("language") == "unknown":
            return base

        def _tup(v: Any) -> tuple[str, ...] | None:
            if v is None:
                return None
            if isinstance(v, (list, tuple)):
                return tuple(v)
            return (str(v),)

        # base profile already computed above from detect_language()

        return LanguageProfile(
            language=data.get("language") or base.language,
            extensions=tuple(data.get("extensions") or base.extensions),
            test_command=tuple(data.get("test_command", base.test_command)),
            test_verify_command=data.get("test_verify_command", base.test_verify_command),
            lint_commands=tuple(data.get("lint_commands", base.lint_commands)),
            lint_check_commands=tuple(data.get("lint_check_commands", base.lint_check_commands)),
            syntax_check_command=(
                _tup(data.get("syntax_check_command")) if "syntax_check_command" in data else base.syntax_check_command
            ),
            package_install_prefix=data.get("package_install_prefix", base.package_install_prefix),
            lsp_language_id=data.get("lsp_language_id", base.lsp_language_id),
            skip_dirs=tuple(data.get("skip_dirs", base.skip_dirs)),
            manifest_files=tuple(data.get("manifest_files", base.manifest_files)),
            prompt_hints=data.get("prompt_hints", base.prompt_hints),
            coverage_command=(
                _tup(data.get("coverage_command")) if "coverage_command" in data else base.coverage_command
            ),
            security_command=(
                _tup(data.get("security_command")) if "security_command" in data else base.security_command
            ),
            required_project_files=tuple(data.get("required_project_files", base.required_project_files)),
        )

    @staticmethod
    def _emit_report(report: QualityReport, assessment: Assessment | None = None) -> None:
        lines = [f"Score: {report.score:.3f} | Errors: {report.total_errors} | Warnings: {report.total_warnings}"]
        if assessment:
            lines.append(f"Overall: {assessment.overall_status().upper()}")
        for idx, (name, pr) in enumerate(report.principles.items()):
            if idx > 0:
                lines.append("")
            status_tag = "PASS" if pr.passed else "FAIL"
            tier = ""
            if assessment and name in assessment.pillars:
                tier = f" ({assessment.pillars[name].status})"
            lines.append(f"  [{status_tag}] {name}: {pr.score:.3f}{tier}")
            for issue in pr.issues[:10]:
                lines.append(f"       - [{issue.severity}] {issue.message}")
        emit_block(M.QRPT, "TRUST 5 Quality Report", "\n".join(lines), max_lines=50)

    @staticmethod
    def _format_quality_feedback(
        report: QualityReport,
        config: QualityConfig,
        phase_issues: list[Any] | None = None,
    ) -> str:
        parts = [
            f"TRUST 5 QUALITY GATE FAILED (score={report.score:.3f}, threshold={config.pass_score_threshold})\n",
            "Fix the following quality issues:\n",
        ]
        for name, pr in report.principles.items():
            if not pr.passed or pr.issues:
                parts.append(f"\n## {name.upper()} (score={pr.score:.3f})")
                for issue in pr.issues:
                    if issue.severity in ("error", "warning"):
                        loc = f" [{issue.file}:{issue.line}]" if issue.file else ""
                        parts.append(f"  - [{issue.severity}]{loc} {issue.message}")
        if phase_issues:
            parts.append("\n## PHASE GATE VIOLATIONS")
            for pi in phase_issues:
                parts.append(f"  - [{pi.severity}] {pi.message}")
        parts.append("\nFix these issues and ensure all tests still pass.")
        return "\n".join(parts)
