"""Phase-specific quality gates, regression detection, and methodology validation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .config import QualityConfig
from .quality import Issue, QualityReport

CONVENTIONAL_COMMIT_RE = re.compile(
    r"^(feat|fix|build|chore|ci|docs|style|refactor|perf|test)" r"(\([a-zA-Z0-9_./-]+\))?!?: .+$"
)

PILLAR_PASS_THRESHOLD = 0.85
PILLAR_WARNING_THRESHOLD = 0.50


@dataclass
class DiagnosticSnapshot:
    """Snapshot of project diagnostics at a point in time for regression detection."""

    errors: int = 0
    warnings: int = 0
    type_errors: int = 0
    lint_errors: int = 0
    security_warnings: int = 0
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class PillarStatus:
    """Enum-like class mapping pillar scores to PASS / WARNING / CRITICAL status."""

    PASS = "pass"
    WARNING = "warning"
    CRITICAL = "critical"

    @staticmethod
    def from_score(score: float) -> str:
        if score >= PILLAR_PASS_THRESHOLD:
            return PillarStatus.PASS
        if score >= PILLAR_WARNING_THRESHOLD:
            return PillarStatus.WARNING
        return PillarStatus.CRITICAL


@dataclass
class PillarAssessment:
    """Assessment result for a single TRUST 5 pillar."""

    pillar: str
    score: float
    status: str
    issues: list[str] = field(default_factory=list)


class Assessment:
    """Aggregated assessment across all TRUST 5 pillars for a QualityReport."""

    def __init__(self, report: QualityReport) -> None:
        self.pillars: dict[str, PillarAssessment] = {}
        for name, pr in report.principles.items():
            self.pillars[name] = PillarAssessment(
                pillar=name,
                score=pr.score,
                status=PillarStatus.from_score(pr.score),
                issues=[i.message for i in pr.issues if i.severity in ("error", "warning")],
            )

    def overall_status(self) -> str:
        if any(p.status == PillarStatus.CRITICAL for p in self.pillars.values()):
            return PillarStatus.CRITICAL
        if any(p.status == PillarStatus.WARNING for p in self.pillars.values()):
            return PillarStatus.WARNING
        return PillarStatus.PASS

    def is_pass(self) -> bool:
        return all(p.score >= PILLAR_PASS_THRESHOLD for p in self.pillars.values())


def is_conventional_commit(msg: str) -> bool:
    """Return True if *msg* matches Conventional Commits format."""
    return bool(CONVENTIONAL_COMMIT_RE.match(msg.strip().split("\n")[0]))


def validate_plan_phase(snapshot: DiagnosticSnapshot) -> list[Issue]:
    """Check diagnostics against plan-phase baseline thresholds."""
    issues: list[Issue] = []
    if snapshot.errors > 0:
        issues.append(
            Issue(
                severity="warning",
                message=f"plan phase baseline: {snapshot.errors} pre-existing errors detected",
                rule="phase-plan-baseline",
            )
        )
    if snapshot.type_errors > 0:
        issues.append(
            Issue(
                severity="warning",
                message=f"plan phase baseline: {snapshot.type_errors} pre-existing type errors detected",
                rule="phase-plan-type-baseline",
            )
        )
    return issues


def validate_run_phase(snapshot: DiagnosticSnapshot, config: QualityConfig) -> list[Issue]:
    """Check diagnostics against run-phase error/lint/type thresholds."""
    issues: list[Issue] = []
    gate = config.run_gate
    if snapshot.errors > gate.max_errors:
        issues.append(
            Issue(
                severity="error",
                message=f"run phase: {snapshot.errors} errors (max {gate.max_errors})",
                rule="phase-run-errors",
            )
        )
    if snapshot.type_errors > gate.max_type_errors:
        issues.append(
            Issue(
                severity="error",
                message=f"run phase: {snapshot.type_errors} type errors (max {gate.max_type_errors})",
                rule="phase-run-type-errors",
            )
        )
    if snapshot.lint_errors > gate.max_lint_errors:
        issues.append(
            Issue(
                severity="error",
                message=f"run phase: {snapshot.lint_errors} lint errors (max {gate.max_lint_errors})",
                rule="phase-run-lint-errors",
            )
        )
    return issues


def validate_sync_phase(snapshot: DiagnosticSnapshot, config: QualityConfig) -> list[Issue]:
    """Check diagnostics against sync-phase error and warning thresholds."""
    issues: list[Issue] = []
    gate = config.sync_gate
    if snapshot.errors > gate.max_errors:
        issues.append(
            Issue(
                severity="error",
                message=f"sync phase: {snapshot.errors} errors (max {gate.max_errors})",
                rule="phase-sync-errors",
            )
        )
    if snapshot.warnings > gate.max_warnings:
        issues.append(
            Issue(
                severity="warning",
                message=f"sync phase: {snapshot.warnings} warnings (max {gate.max_warnings})",
                rule="phase-sync-warnings",
            )
        )
    return issues


def validate_phase(
    phase: str,
    snapshot: DiagnosticSnapshot,
    config: QualityConfig,
) -> list[Issue]:
    """Dispatch to the correct phase validator based on *phase* name."""
    if phase == "plan":
        return validate_plan_phase(snapshot)
    if phase == "run":
        return validate_run_phase(snapshot, config)
    if phase == "sync":
        return validate_sync_phase(snapshot, config)
    return []


def detect_regression(
    baseline: DiagnosticSnapshot,
    current: DiagnosticSnapshot,
    config: QualityConfig,
) -> list[Issue]:
    """Compare *current* snapshot to *baseline* and flag regressions."""
    issues: list[Issue] = []
    reg = config.regression

    err_inc = current.errors - baseline.errors
    if err_inc > reg.error_increase_threshold:
        issues.append(
            Issue(
                severity="error",
                message=f"errors increased {baseline.errors} -> {current.errors} "
                f"(threshold {reg.error_increase_threshold})",
                rule="regression-errors",
            )
        )

    warn_inc = current.warnings - baseline.warnings
    if warn_inc > reg.warning_increase_threshold:
        issues.append(
            Issue(
                severity="warning",
                message=f"warnings increased {baseline.warnings} -> {current.warnings} "
                f"(threshold {reg.warning_increase_threshold})",
                rule="regression-warnings",
            )
        )

    te_inc = current.type_errors - baseline.type_errors
    if te_inc > reg.type_error_increase_threshold:
        issues.append(
            Issue(
                severity="error",
                message=f"type errors increased {baseline.type_errors} -> {current.type_errors} "
                f"(threshold {reg.type_error_increase_threshold})",
                rule="regression-type-errors",
            )
        )

    return issues


@dataclass
class MethodologyContext:
    """Context for methodology-specific validation (DDD, TDD, hybrid)."""

    characterization_tests_exist: bool = False
    preserve_step_completed: bool = False
    behavior_snapshot_regressed: bool = False
    test_first_verified: bool = False
    commit_coverage: int = 0
    coverage_exemption_requested: bool = False
    new_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    new_code_coverage: int = 0
    legacy_code_coverage: int = 0
    # Oracle Problem mitigation fields
    assertion_density: float = -1.0  # -1.0 = not measured, 0.0-1.0 = ratio
    mutation_score: float = -1.0  # -1.0 = not measured, 0.0-1.0 = kill rate


def validate_methodology(
    mode: str,
    ctx: MethodologyContext,
    config: QualityConfig,
) -> list[Issue]:
    """Validate development methodology constraints and Oracle Problem mitigations."""
    if mode == "ddd":
        issues = _validate_ddd(ctx, config)
    elif mode == "tdd":
        issues = _validate_tdd(ctx, config)
    elif mode == "hybrid":
        issues = _validate_hybrid(ctx, config)
    else:
        issues = []
    # Oracle Problem checks apply to ALL modes
    issues.extend(_validate_oracle_mitigations(ctx, config))
    return issues


def _validate_ddd(ctx: MethodologyContext, config: QualityConfig) -> list[Issue]:
    issues: list[Issue] = []
    ddd = config.ddd
    if ddd.characterization_tests and not ctx.characterization_tests_exist:
        issues.append(
            Issue(
                severity="error",
                message="characterization tests required for modified files in DDD mode",
                rule="ddd-characterization",
            )
        )
    if ddd.preserve_before_improve and not ctx.preserve_step_completed:
        issues.append(
            Issue(
                severity="error",
                message="PRESERVE step must complete before IMPROVE",
                rule="ddd-preserve-before-improve",
            )
        )
    if ctx.behavior_snapshot_regressed:
        issues.append(
            Issue(
                severity="error",
                message="behavior snapshot regression detected",
                rule="ddd-behavior-snapshot",
            )
        )
    return issues


def _validate_tdd(ctx: MethodologyContext, config: QualityConfig) -> list[Issue]:
    issues: list[Issue] = []
    tdd = config.tdd
    if tdd.require_test_first and not ctx.test_first_verified:
        issues.append(
            Issue(
                severity="error",
                message="tests must be written before implementation in TDD mode",
                rule="tdd-test-first",
            )
        )
    if ctx.coverage_exemption_requested:
        issues.append(
            Issue(
                severity="error",
                message="coverage exemptions not allowed in TDD mode",
                rule="tdd-no-exemption",
            )
        )
    if tdd.min_coverage_per_commit > 0 and ctx.commit_coverage < tdd.min_coverage_per_commit:
        issues.append(
            Issue(
                severity="error",
                message=f"commit coverage {ctx.commit_coverage}% below TDD minimum {tdd.min_coverage_per_commit}%",
                rule="tdd-min-coverage",
            )
        )
    return issues


def _validate_hybrid(ctx: MethodologyContext, config: QualityConfig) -> list[Issue]:
    issues: list[Issue] = []
    hyb = config.hybrid
    if ctx.new_files and hyb.min_coverage_new > 0 and ctx.new_code_coverage < hyb.min_coverage_new:
        issues.append(
            Issue(
                severity="error",
                message=f"new code coverage {ctx.new_code_coverage}% below hybrid minimum {hyb.min_coverage_new}%",
                rule="hybrid-new-coverage",
            )
        )
    if ctx.modified_files and hyb.min_coverage_legacy > 0 and ctx.legacy_code_coverage < hyb.min_coverage_legacy:
        issues.append(
            Issue(
                severity="error",
                message=f"legacy code coverage {ctx.legacy_code_coverage}% below hybrid minimum "
                f"{hyb.min_coverage_legacy}%",
                rule="hybrid-legacy-coverage",
            )
        )
    return issues


def _validate_oracle_mitigations(ctx: MethodologyContext, config: QualityConfig) -> list[Issue]:
    """Validate oracle problem mitigations — applies to ALL development modes."""
    issues: list[Issue] = []
    # Assertion density: flag vacuous test suites
    if ctx.assertion_density >= 0 and ctx.assertion_density < 0.5:
        issues.append(
            Issue(
                severity="error",
                message=f"assertion density {ctx.assertion_density:.0%} — "
                "test suite may contain vacuous tests (Oracle Problem risk)",
                rule="oracle-assertion-density",
            )
        )
    elif ctx.assertion_density >= 0 and ctx.assertion_density < 0.8:
        issues.append(
            Issue(
                severity="warning",
                message=f"assertion density {ctx.assertion_density:.0%} — "
                "some test functions may lack meaningful assertions",
                rule="oracle-assertion-density",
            )
        )
    # Mutation score: flag when mutation testing was run and score is low
    mutation_enabled = config.tdd.mutation_testing_enabled or config.test_quality.mutation_testing_enabled
    if mutation_enabled and ctx.mutation_score >= 0 and ctx.mutation_score < 0.8:
        issues.append(
            Issue(
                severity="error",
                message=f"mutation score {ctx.mutation_score:.0%} — "
                "tests failed to detect injected faults (Oracle Problem detected)",
                rule="oracle-mutation-score",
            )
        )
    elif mutation_enabled and ctx.mutation_score >= 0 and ctx.mutation_score < 0.95:
        issues.append(
            Issue(
                severity="warning",
                message=f"mutation score {ctx.mutation_score:.0%} — some mutations survived the test suite",
                rule="oracle-mutation-score",
            )
        )
    return issues


def build_snapshot_from_report(report: QualityReport) -> DiagnosticSnapshot:
    """Build a DiagnosticSnapshot from a completed QualityReport."""
    snap = DiagnosticSnapshot()
    for pr in report.principles.values():
        for issue in pr.issues:
            if issue.severity == "error":
                snap.errors += 1
                if issue.rule == "type-error":
                    snap.type_errors += 1
                elif issue.rule == "lint-errors":
                    snap.lint_errors += 1
            elif issue.severity == "warning":
                snap.warnings += 1
                if "security" in issue.rule:
                    snap.security_warnings += 1
    return snap
