from __future__ import annotations
from unittest.mock import MagicMock, patch
from stabilize.models.status import WorkflowStatus
from trust5.core.config import QualityConfig
from trust5.core.quality import (
    PrincipleResult,
    QualityReport,
    _filter_excluded_findings,
    _path_in_skip_dirs,
)
from trust5.tasks.quality_task import QualityTask

def make_stage(context: dict | None = None) -> MagicMock:
    stage = MagicMock()
    stage.context = context or {}
    stage.context.setdefault("project_root", "/tmp/fake-project")
    return stage

def _make_report(score: float = 0.90, passed: bool = True, errors: int = 0, warnings: int = 0) -> QualityReport:
    """Create a QualityReport with reasonable defaults."""
    return QualityReport(
        passed=passed,
        score=score,
        principles={
            "tested": PrincipleResult(name="tested", passed=True, score=1.0),
            "readable": PrincipleResult(name="readable", passed=True, score=1.0),
            "understandable": PrincipleResult(name="understandable", passed=True, score=1.0),
            "secured": PrincipleResult(name="secured", passed=True, score=1.0),
            "trackable": PrincipleResult(name="trackable", passed=True, score=1.0),
        },
        total_errors=errors,
        total_warnings=warnings,
        coverage_pct=90.0,
        timestamp="2026-02-15T12:00:00+00:00",
    )

def _make_failing_report(score: float = 0.50) -> QualityReport:
    """Create a failing QualityReport."""
    return QualityReport(
        passed=False,
        score=score,
        principles={
            "tested": PrincipleResult(name="tested", passed=False, score=0.3),
            "readable": PrincipleResult(name="readable", passed=True, score=0.8),
            "understandable": PrincipleResult(name="understandable", passed=True, score=0.7),
            "secured": PrincipleResult(name="secured", passed=True, score=0.9),
            "trackable": PrincipleResult(name="trackable", passed=True, score=0.8),
        },
        total_errors=3,
        total_warnings=2,
        coverage_pct=40.0,
        timestamp="2026-02-15T12:00:00+00:00",
    )
