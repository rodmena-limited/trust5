"""Keyword-based SPEC compliance checker.

Extracts identifiers from EARS acceptance criteria and searches source code
to determine whether each criterion has been addressed. No LLM calls — fast
and deterministic.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Regexes for extracting searchable identifiers from criteria text
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
    status: str  # "met", "partial", "not_met"
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


def extract_identifiers(criterion: str) -> list[str]:
    """Extract searchable identifiers from an EARS criterion string.

    Looks for:
    - PascalCase class names (e.g. MonteCarloSimulator)
    - Backtick-quoted identifiers (e.g. `random_seed`)
    - Double-quoted identifiers (e.g. "batch_size")
    - Long snake_case identifiers (>5 chars, to skip generic words)
    """
    ids: list[str] = []
    seen: set[str] = set()

    def _add(ident: str) -> None:
        lower = ident.lower()
        if lower not in seen:
            seen.add(lower)
            ids.append(ident)

    for m in _PASCAL_CASE_RE.finditer(criterion):
        _add(m.group())

    for m in _BACKTICK_RE.finditer(criterion):
        _add(m.group(1))

    for m in _QUOTED_RE.finditer(criterion):
        val = m.group(1)
        if len(val) > 3 and not val.startswith("http"):
            _add(val)

    for m in _SNAKE_CASE_RE.finditer(criterion):
        val = m.group()
        if len(val) > 5:
            _add(val)

    return ids


def _is_test_file(path: str) -> bool:
    return bool(_TEST_PATTERNS.search(path))


def _read_source_files(
    project_root: str,
    extensions: tuple[str, ...] = (".py",),
    skip_dirs: tuple[str, ...] = (),
) -> str:
    """Read and concatenate all non-test source files."""
    effective_skip = _DEFAULT_SKIP_DIRS | set(skip_dirs)
    ext_set = set(extensions)
    chunks: list[str] = []

    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [d for d in dirnames if d not in effective_skip]
        for fname in filenames:
            _, ext = os.path.splitext(fname)
            if ext not in ext_set:
                continue
            rel_path = os.path.relpath(os.path.join(dirpath, fname), project_root)
            if _is_test_file(rel_path):
                continue
            full_path = os.path.join(dirpath, fname)
            try:
                with open(full_path, encoding="utf-8", errors="replace") as f:
                    chunks.append(f.read())
            except OSError:
                continue

    return "\n".join(chunks)


def check_compliance(
    acceptance_criteria: list[str] | tuple[str, ...],
    project_root: str,
    extensions: tuple[str, ...] = (".py",),
    skip_dirs: tuple[str, ...] = (),
) -> ComplianceReport:
    """Check source code compliance against acceptance criteria.

    For each criterion, extracts identifiers and searches source text.
    A criterion is ``met`` if >=50% of its identifiers are found,
    ``partial`` if >0 but <50%, ``not_met`` if none are found.

    Returns a neutral report (ratio=1.0) when no criteria are provided.
    """
    if not acceptance_criteria:
        return ComplianceReport(
            criteria_total=0,
            criteria_met=0,
            criteria_not_met=0,
            compliance_ratio=1.0,
        )

    source_text = _read_source_files(project_root, extensions, skip_dirs)
    source_lower = source_text.lower()

    results: list[CriterionResult] = []
    met_count = 0
    not_met_count = 0
    unmet: list[str] = []

    for criterion in acceptance_criteria:
        identifiers = extract_identifiers(criterion)

        if not identifiers:
            # No extractable identifiers — can't verify, assume met
            results.append(CriterionResult(
                criterion=criterion,
                status="met",
                matched_identifiers=(),
                searched_identifiers=(),
            ))
            met_count += 1
            continue

        matched: list[str] = []
        for ident in identifiers:
            if ident.lower() in source_lower:
                matched.append(ident)

        ratio = len(matched) / len(identifiers)
        if ratio >= 0.5:
            status = "met"
            met_count += 1
        elif matched:
            status = "partial"
            not_met_count += 1
            unmet.append(criterion)
        else:
            status = "not_met"
            not_met_count += 1
            unmet.append(criterion)

        results.append(CriterionResult(
            criterion=criterion,
            status=status,
            matched_identifiers=tuple(matched),
            searched_identifiers=tuple(identifiers),
        ))

    total = len(acceptance_criteria)
    compliance_ratio = met_count / total if total > 0 else 1.0

    return ComplianceReport(
        criteria_total=total,
        criteria_met=met_count,
        criteria_not_met=not_met_count,
        compliance_ratio=compliance_ratio,
        results=tuple(results),
        unmet_criteria=tuple(unmet),
    )
