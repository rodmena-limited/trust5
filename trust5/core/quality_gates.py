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

def is_conventional_commit(msg: str) -> bool:
    return bool(CONVENTIONAL_COMMIT_RE.match(msg.strip().split("\n")[0]))

def validate_plan_phase(snapshot: DiagnosticSnapshot) -> list[Issue]:
    return []

def validate_run_phase(snapshot: DiagnosticSnapshot, config: QualityConfig) -> list[Issue]:
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
    if phase == "plan":
        return validate_plan_phase(snapshot)
    if phase == "run":
        return validate_run_phase(snapshot, config)
    if phase == "sync":
        return validate_sync_phase(snapshot, config)
    return []

@dataclass
class DiagnosticSnapshot:
    errors: int = 0
    warnings: int = 0
    type_errors: int = 0
    lint_errors: int = 0
    security_warnings: int = 0
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

class PillarStatus:
    PASS = 'pass'
    WARNING = 'warning'
    CRITICAL = 'critical'

    def from_score(score: float) -> str:
        if score >= PILLAR_PASS_THRESHOLD:
            return PillarStatus.PASS
        if score >= PILLAR_WARNING_THRESHOLD:
            return PillarStatus.WARNING
        return PillarStatus.CRITICAL

@dataclass
class PillarAssessment:
    pillar: str
    score: float
    status: str
    issues: list[str] = field(default_factory=list)

class Assessment:
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
