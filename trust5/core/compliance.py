"""SPEC compliance checker — LLM-based with keyword fallback.

Primary: sends source code + acceptance criteria to an LLM, which judges
each criterion as met/partial/not_met.  Language-agnostic and accurate.

Fallback: when no LLM is available (tests, offline), uses keyword extraction
and string matching — fast and deterministic but imprecise for EARS criteria.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .llm import LLM

logger = logging.getLogger(__name__)

# ── Regex patterns for keyword-based fallback ────────────────────────────────
_PASCAL_CASE_RE = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_QUOTED_RE = re.compile(r'"([^"]+)"')
_SNAKE_CASE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

_DEFAULT_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".tox",
        ".eggs",
        "*.egg-info",
    }
)

_TEST_PATTERNS = re.compile(
    r"(^|/)tests?/|test_[^/]*\.py$|_test\.(py|ts|js|go|rs)$|\.spec\.(ts|js)$",
)

# Max chars of source code to send to the LLM for compliance checking.
# Large codebases are truncated to avoid token limits.
_MAX_SOURCE_CHARS = 80_000

# JSON extraction from LLM response
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n\s*```", re.DOTALL)
_BARE_JSON_RE = re.compile(r"\{[^{}]*\"criteria\"[^{}]*\[.*?\]\s*\}", re.DOTALL)


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


# ── LLM-based compliance assessment ─────────────────────────────────────────


_COMPLIANCE_SYSTEM_PROMPT = """\
You are a SPEC compliance auditor. You verify whether source code implements \
acceptance criteria defined in a SPEC document.

RULES:
- Judge ONLY by reading the source code provided.
- A criterion is "met" if the code clearly implements the required behavior.
- A criterion is "partial" if the code partially addresses it but is incomplete.
- A criterion is "not_met" if there is no evidence of implementation.
- Ignore EARS tags like [UBIQ], [EVENT], [STATE], [OPTIONAL] — focus on the \
requirement itself.
- Be generous: if the code has the right structure/patterns for a criterion, \
mark it "met" even if you can't execute it.

OUTPUT FORMAT (strict JSON, no commentary before or after):
```json
{
  "criteria": [
    {"index": 0, "status": "met", "evidence": "brief reason"},
    {"index": 1, "status": "not_met", "evidence": "brief reason"},
    ...
  ]
}
```

Each entry must have "index" (0-based, matching the criteria list order), \
"status" ("met", "partial", or "not_met"), and "evidence" (1 sentence max).
"""


def _build_compliance_prompt(
    criteria: list[str] | tuple[str, ...],
    source_text: str,
) -> str:
    """Build the user prompt for LLM compliance checking."""
    criteria_block = "\n".join(f"  {i}. {c}" for i, c in enumerate(criteria))

    # Truncate source if too large
    if len(source_text) > _MAX_SOURCE_CHARS:
        source_text = source_text[:_MAX_SOURCE_CHARS] + "\n\n[... truncated ...]"

    return (
        f"## Acceptance Criteria\n\n{criteria_block}\n\n"
        f"## Source Code\n\n```\n{source_text}\n```\n\n"
        f"Judge each criterion against the source code. "
        f"Return ONLY the JSON block, no other text."
    )


def _parse_llm_response(
    raw: str,
    criteria: list[str] | tuple[str, ...],
) -> list[CriterionResult]:
    """Parse the LLM's JSON response into CriterionResult objects.

    Handles: code-fenced JSON, bare JSON, and malformed responses.
    """
    # Try code-fenced JSON first
    match = _JSON_BLOCK_RE.search(raw)
    json_str = match.group(1) if match else None

    if json_str is None:
        # Try bare JSON
        match = _BARE_JSON_RE.search(raw)
        json_str = match.group(0) if match else None

    if json_str is None:
        # Last resort: try entire response as JSON
        json_str = raw.strip()

    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        logger.warning("LLM compliance response was not valid JSON, falling back to keyword method")
        return []

    items = data.get("criteria", [])
    if not isinstance(items, list):
        logger.warning("LLM compliance response missing 'criteria' list")
        return []

    results: list[CriterionResult] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        idx = item.get("index", -1)
        status = str(item.get("status", "not_met")).lower()
        evidence = str(item.get("evidence", ""))

        if status not in ("met", "partial", "not_met"):
            status = "not_met"

        if not isinstance(idx, int) or idx < 0 or idx >= len(criteria):
            continue

        results.append(
            CriterionResult(
                criterion=criteria[idx],
                status=status,
                matched_identifiers=(evidence,) if evidence else (),
                searched_identifiers=("llm-assessed",),
            )
        )

    return results


def _check_compliance_llm(
    acceptance_criteria: list[str] | tuple[str, ...],
    source_text: str,
    llm: LLM,
) -> ComplianceReport | None:
    """Use an LLM to assess SPEC compliance. Returns None on failure."""
    from .message import M, emit

    prompt = _build_compliance_prompt(acceptance_criteria, source_text)

    try:
        response = llm.chat(
            messages=[
                {"role": "system", "content": _COMPLIANCE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            tools=None,
        )
    except Exception as e:
        logger.warning("LLM compliance check failed: %s", e)
        emit(M.SWRN, f"LLM compliance check failed: {e} — falling back to keyword matching")
        return None

    message = response.get("message", {})
    content: str = message.get("content", "")

    if not content:
        logger.warning("LLM compliance check returned empty response")
        return None

    results = _parse_llm_response(content, acceptance_criteria)
    if not results:
        return None

    # Build index-keyed set for gap detection
    assessed_indices = {
        i for i, _ in enumerate(acceptance_criteria) if any(r.criterion == acceptance_criteria[i] for r in results)
    }

    # Fill in any missing criteria (LLM may have skipped some)
    for i, criterion in enumerate(acceptance_criteria):
        if i not in assessed_indices:
            results.append(
                CriterionResult(
                    criterion=criterion,
                    status="not_met",
                    matched_identifiers=("LLM did not assess this criterion",),
                    searched_identifiers=("llm-assessed",),
                )
            )

    met_count = sum(1 for r in results if r.status == "met")
    not_met_count = sum(1 for r in results if r.status != "met")
    total = len(acceptance_criteria)
    unmet = tuple(r.criterion for r in results if r.status != "met")

    return ComplianceReport(
        criteria_total=total,
        criteria_met=met_count,
        criteria_not_met=not_met_count,
        compliance_ratio=met_count / total if total > 0 else 1.0,
        results=tuple(results),
        unmet_criteria=unmet,
    )


# ── Keyword-based fallback ───────────────────────────────────────────────────


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
                    chunks.append(f"# --- {rel_path} ---\n{f.read()}")
            except OSError:
                continue

    return "\n\n".join(chunks)


def _check_compliance_keywords(
    acceptance_criteria: list[str] | tuple[str, ...],
    source_text: str,
) -> ComplianceReport:
    """Keyword-based compliance check (deterministic fallback)."""
    source_lower = source_text.lower()

    results: list[CriterionResult] = []
    met_count = 0
    not_met_count = 0
    unmet: list[str] = []

    for criterion in acceptance_criteria:
        identifiers = extract_identifiers(criterion)

        if not identifiers:
            # No extractable identifiers — can't verify, assume met
            results.append(
                CriterionResult(
                    criterion=criterion,
                    status="met",
                    matched_identifiers=(),
                    searched_identifiers=(),
                )
            )
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

        results.append(
            CriterionResult(
                criterion=criterion,
                status=status,
                matched_identifiers=tuple(matched),
                searched_identifiers=tuple(identifiers),
            )
        )

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


# ── Public API ───────────────────────────────────────────────────────────────


def check_compliance(
    acceptance_criteria: list[str] | tuple[str, ...],
    project_root: str,
    extensions: tuple[str, ...] = (".py",),
    skip_dirs: tuple[str, ...] = (),
    llm: LLM | None = None,
) -> ComplianceReport:
    """Check source code compliance against acceptance criteria.

    When *llm* is provided, uses LLM-based semantic assessment (recommended).
    Falls back to keyword matching if the LLM call fails or is not provided.

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

    if not source_text.strip():
        # No source files found — all criteria are unmet
        return ComplianceReport(
            criteria_total=len(acceptance_criteria),
            criteria_met=0,
            criteria_not_met=len(acceptance_criteria),
            compliance_ratio=0.0,
            results=tuple(
                CriterionResult(
                    criterion=c,
                    status="not_met",
                    matched_identifiers=(),
                    searched_identifiers=(),
                )
                for c in acceptance_criteria
            ),
            unmet_criteria=tuple(acceptance_criteria),
        )

    # Primary: LLM-based assessment
    if llm is not None:
        report = _check_compliance_llm(acceptance_criteria, source_text, llm)
        if report is not None:
            return report
        # LLM failed — fall through to keyword fallback

    # Fallback: keyword matching
    return _check_compliance_keywords(acceptance_criteria, source_text)
