"""TRUST 5 quality gate framework for Correcto.

This module is a backward-compatible facade. All models, constants,
and utility functions live in ``quality_models``; all validator classes
live in ``quality_validators``.
"""

from __future__ import annotations

from typing import Any

from .config import QualityConfig
from .quality_models import (  # noqa: F401  # noqa: F401  # noqa: F401
    _ASSERTION_PATTERNS,
    _SKIP_SIZE_CHECK,
    _TEST_FUNC_PATTERNS,
    _TEST_PATTERN,
    _TOOL_MISSING_PATTERNS,
    ALL_PRINCIPLES,
    MAX_FILE_LINES,
    PASS_SCORE_THRESHOLD,
    PRINCIPLE_READABLE,
    PRINCIPLE_SECURED,
    PRINCIPLE_TESTED,
    PRINCIPLE_TRACKABLE,
    PRINCIPLE_UNDERSTANDABLE,
    PRINCIPLE_WEIGHTS,
    SUBPROCESS_TIMEOUT,
    PrincipleResult,
    _check_doc_completeness,
    _check_file_sizes,
    _check_generic_assertions,
    _check_python_assertions,
    _filter_excluded_findings,
    _find_source_files,
    _has_python_assertions,
    _is_tool_missing,
    _parse_coverage,
    _parse_security_json,
    _path_in_skip_dirs,
    _run_command,
    check_assertion_density,
)
from .quality_models import Issue as Issue
from .quality_models import QualityReport as QualityReport
from .quality_validators import (  # noqa: F401  # noqa: F401
    ReadableValidator,
    SecuredValidator,
    TestedValidator,
    TrackableValidator,
    UnderstandableValidator,
    _ValidatorBase,
)
from .quality_validators import TrustGate as TrustGate


def meets_quality_gate(report: QualityReport, config: QualityConfig) -> bool:
    """Return True if *report* meets quality thresholds in *config*."""
    if not report.passed or report.total_errors > config.max_errors:
        return False
    return not (report.coverage_pct >= 0 and report.coverage_pct < config.coverage_threshold)


def is_improved(prev: dict[str, Any] | None, curr: QualityReport) -> bool:
    """Return True if *curr* report improved over *prev* (higher score or fewer errors)."""
    if prev is None:
        return False
    return bool(curr.score > prev.get("score", 0.0) or curr.total_errors < prev.get("total_errors", 999))


def is_stagnant(prev: dict[str, Any] | None, curr: QualityReport) -> bool:
    """Return True if *curr* report is identical to *prev* (no progress between attempts)."""
    if prev is None:
        return False
    return bool(
        curr.score == prev.get("score", -1)
        and curr.total_errors == prev.get("total_errors", -1)
        and curr.total_warnings == prev.get("total_warnings", -1)
    )
