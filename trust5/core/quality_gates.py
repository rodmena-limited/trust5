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
