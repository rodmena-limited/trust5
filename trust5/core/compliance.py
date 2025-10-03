from __future__ import annotations
import logging
import os
import re
from dataclasses import dataclass
logger = logging.getLogger(__name__)
_PASCAL_CASE_RE = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_QUOTED_RE = re.compile(r'"([^"]+)"')
_SNAKE_CASE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_DEFAULT_SKIP_DIRS = frozenset({
    "__pycache__", ".venv", "venv", "node_modules", ".git",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    ".tox", ".eggs", "*.egg-info",
})
_TEST_PATTERNS = re.compile(
    r"(^|/)tests?/|test_[^/]*\.py$|_test\.(py|ts|js|go|rs)$|\.spec\.(ts|js)$",
)

@dataclass(frozen=True)
class CriterionResult:
    """Result of checking a single acceptance criterion against source code."""
    criterion: str
    status: str
    matched_identifiers: tuple[str, ...]
    searched_identifiers: tuple[str, ...]

@dataclass(frozen=True)
class ComplianceReport:
    """Aggregated compliance check results."""
    criteria_total: int
    criteria_met: int
    criteria_not_met: int
    compliance_ratio: float
    results: tuple[CriterionResult, ...] = ()
    unmet_criteria: tuple[str, ...] = ()
