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
