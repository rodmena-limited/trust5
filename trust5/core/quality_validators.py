"""TRUST 5 quality gate validator classes."""

from __future__ import annotations

import logging
import os
import re
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

from ..tasks.validate_helpers import _exclude_test_files_from_lint_cmd, _filter_test_file_lint
from .config import QualityConfig
from .lang import LanguageProfile
from .message import M, emit
from .quality_models import (
    _TEST_PATTERN,
    ALL_PRINCIPLES,
    MAX_FILE_LINES,
    PRINCIPLE_COMPLETENESS,
    PRINCIPLE_READABLE,
    PRINCIPLE_SECURED,
    PRINCIPLE_TESTED,
    PRINCIPLE_TRACKABLE,
    PRINCIPLE_UNDERSTANDABLE,
    PRINCIPLE_WEIGHTS,
    Issue,
    PrincipleResult,
    QualityReport,
    _check_doc_completeness,
    _check_file_sizes,
    _filter_excluded_findings,
    _find_source_files,
    _is_tool_missing,
    _parse_coverage,
    _parse_security_json,
    _run_command,
    check_assertion_density,
)

logger = logging.getLogger(__name__)


# ── Validator base ───────────────────────────────────────────────────


class _ValidatorBase(ABC):
    """Abstract base for TRUST 5 pillar validators.

    Subclasses implement ``name()`` → pillar name and ``validate()`` → PrincipleResult.
    """

    def __init__(self, project_root: str, profile: LanguageProfile, config: QualityConfig):
        self._root = project_root
        self._profile = profile
        self._config = config

    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def validate(self) -> PrincipleResult:
        raise NotImplementedError


# ── TestedValidator ──────────────────────────────────────────────────


class TestedValidator(_ValidatorBase):
    """Validates the *Tested* pillar: test pass rate, type errors, coverage, assertion density."""

    def name(self) -> str:
        return PRINCIPLE_TESTED

    def validate(self) -> PrincipleResult:
        result = PrincipleResult(name=self.name(), passed=True, score=1.0)
        checks, score = 4.0, 0.0

        plan_test = self._config.plan_test_command
        if plan_test:
            test_cmd: tuple[str, ...] = ("sh", "-c", plan_test)
        else:
            test_cmd = self._profile.test_command
        rc_test, out_test = _run_command(test_cmd, self._root)
        if rc_test == 0:
            score += 1.0
        else:
            result.issues.append(Issue(severity="error", message="tests failed", rule="tests-pass"))

        type_errors = len(re.findall(r"(?i)type\s*error", out_test))
        if type_errors == 0:
            score += 1.0
        else:
            result.issues.append(
                Issue(
                    severity="error",
                    message=f"{type_errors} type error(s)",
                    rule="type-error",
                )
            )

        cov = -1.0
        plan_cov = self._config.plan_coverage_command
        if plan_cov:
            cov_cmd: tuple[str, ...] | None = ("sh", "-c", plan_cov)
        else:
            cov_cmd = self._profile.coverage_command
        rc_cov, out_cov = _run_command(cov_cmd, self._root)
        if rc_cov == 127:
            score += 0.5
            result.issues.append(
                Issue(
                    severity="hint",
                    message="coverage tool not available",
                    rule="coverage-unavailable",
                )
            )
        else:
            cov = _parse_coverage(out_cov, self._profile.language)
            if cov < 0:
                score += 0.5
                result.issues.append(
                    Issue(
                        severity="hint",
                        message="coverage output unparseable",
                        rule="coverage-parse-fail",
                    )
                )
            elif cov >= self._config.coverage_threshold:
                score += 1.0
            else:
                score += min(1.0, cov / self._config.coverage_threshold)
                result.issues.append(
                    Issue(
                        severity="error",
                        message=f"coverage {cov:.1f}% below {self._config.coverage_threshold}%",
                        rule="coverage-threshold",
                    )
                )

        if cov >= 0:
            result.issues.append(
                Issue(
                    severity="hint",
                    message=f"coverage={cov:.1f}%",
                    rule="coverage-measured",
                )
            )

        # Check 4: Assertion density (Oracle Problem mitigation)
        assertion_density, assertion_issues = check_assertion_density(
            self._root,
            self._profile.extensions,
            self._profile.skip_dirs,
            self._profile.language,
        )
        score += assertion_density
        result.issues.extend(assertion_issues)
        result.issues.append(
            Issue(
                severity="hint",
                message=f"assertion_density={assertion_density:.2f}",
                rule="assertion-density-measured",
            )
        )

        result.score = round(score / checks, 3)
        result.passed = (
            rc_test == 0
            and type_errors == 0
            and (cov < 0 or cov >= self._config.coverage_threshold)
            and assertion_density >= 0.5
        )
        return result


# ── ReadableValidator ────────────────────────────────────────────────


class ReadableValidator(_ValidatorBase):
    """LLM-first readable validator.

    Runs lint commands, captures raw output. Does NOT regex-parse errors.
    Raw output is stored as Issue.message so the repair agent (LLM) can
    interpret it in context. Scoring is based on exit codes, not parsed
    violation counts.
    """

    def name(self) -> str:
        return PRINCIPLE_READABLE

    def validate(self) -> PrincipleResult:
        result = PrincipleResult(name=self.name(), passed=True, score=1.0)
        lint_failures = 0

        plan_lint = self._config.plan_lint_command
        if plan_lint:
            cmds: tuple[str, ...] = (plan_lint,)
        else:
            cmds = self._profile.lint_check_commands or self._profile.lint_commands

        lang = self._profile.language
        for cmd_str in cmds:
            # Exclude test files from lint scan before execution
            cmd_str = _exclude_test_files_from_lint_cmd(cmd_str, lang)
            rc, out = _run_command(("sh", "-c", cmd_str), self._root)
            if rc == 0:
                continue
            if rc == 127 or _is_tool_missing(out):
                continue
            # Filter test-file lint errors from output (same as validate_task)
            out = _filter_test_file_lint(out)
            if not out.strip():
                # All lint errors were in test files — treat as clean
                continue
            lint_failures += 1
            result.issues.append(
                Issue(
                    severity="error",
                    message=out[:2000],
                    rule="lint-errors",
                )
            )

        result.score = max(0.0, round(1.0 - lint_failures * 0.2, 3))
        result.passed = lint_failures == 0
        return result


# ── UnderstandableValidator ──────────────────────────────────────────


class UnderstandableValidator(_ValidatorBase):
    """Validates the *Understandable* pillar: warnings, file sizes, documentation completeness."""

    def name(self) -> str:
        return PRINCIPLE_UNDERSTANDABLE

    def validate(self) -> PrincipleResult:
        result = PrincipleResult(name=self.name(), passed=True, score=1.0)
        checks = 3.0
        score = 0.0

        warnings = 0
        skip = set(self._profile.skip_dirs)
        for cmd_str in self._profile.lint_commands:
            _, out = _run_command(("sh", "-c", cmd_str), self._root)
            for line in out.splitlines():
                if not re.search(r"warning", line, re.IGNORECASE):
                    continue
                # Skip warnings originating from excluded directories
                if skip and any(
                    f"/{d}/" in line or line.startswith(f"{d}/") or line.startswith(f"./{d}/") for d in skip
                ):
                    continue
                # Skip warnings from test files
                if _TEST_PATTERN.search(line):
                    continue
                warnings += 1

        threshold = self._config.max_warnings
        if threshold > 0 and warnings > threshold:
            result.issues.append(
                Issue(
                    severity="warning",
                    message=f"warning count {warnings} exceeds threshold {threshold}",
                    rule="warnings-threshold",
                )
            )
            score += max(0.0, 1.0 - (warnings - threshold) * 0.05)
        else:
            score += 1.0

        source_files = _find_source_files(self._root, self._profile.extensions, self._profile.skip_dirs)
        non_test_files = [f for f in source_files if not _TEST_PATTERN.search(os.path.basename(f))]
        max_lines = self._config.max_file_lines or MAX_FILE_LINES
        size_issues = _check_file_sizes(non_test_files, max_lines)
        if size_issues:
            result.issues.extend(size_issues)
            score += 0.5
        else:
            score += 1.0

        doc_score = _check_doc_completeness(non_test_files, self._profile.language)
        if doc_score < 0.5:
            result.issues.append(
                Issue(
                    severity="warning",
                    message=f"documentation completeness {doc_score:.0%} is low",
                    rule="doc-completeness",
                )
            )
            score += doc_score
        else:
            score += 1.0

        result.score = round(score / checks, 3)
        result.passed = (threshold == 0 or warnings <= threshold) and len(size_issues) == 0
        return result


# ── SecuredValidator ─────────────────────────────────────────────────


class SecuredValidator(_ValidatorBase):
    """Validates the *Secured* pillar: runs security scanners and classifies findings."""

    def name(self) -> str:
        return PRINCIPLE_SECURED

    def validate(self) -> PrincipleResult:
        result = PrincipleResult(name=self.name(), passed=True, score=1.0)
        if self._profile.security_command is None:
            result.issues.append(
                Issue(
                    severity="hint",
                    message="no security scanner configured",
                    rule="security-unavailable",
                )
            )
            return result

        rc, out = _run_command(self._profile.security_command, self._root)
        if rc == 127:
            result.issues.append(
                Issue(
                    severity="hint",
                    message=f"security tool not installed: {self._profile.security_command[0]}",
                    rule="security-unavailable",
                )
            )
            return result

        # Parse findings -- try JSON first, then minimal fallback
        findings = _parse_security_json(out)
        findings = _filter_excluded_findings(findings, self._profile.skip_dirs)
        # Filter out test file findings — test code has different security standards
        findings = [f for f in findings if not _TEST_PATTERN.search(os.path.basename(f.get("file", "")))]
        if not findings and rc != 0:
            # Only flag genuine CVE references -- avoid matching metric summaries
            # or JSON keys like "SEVERITY.HIGH": 0 which are NOT findings.
            _json_chars = ("{", "}", "\x22", "\x27")
            for line in out.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith(_json_chars):
                    continue
                if re.search(r"CVE-\d{4}-\d+", stripped):
                    findings.append(
                        {
                            "sev": "HIGH",
                            "text": stripped,
                            "file": "",
                            "line": "0",
                            "rule": "cve",
                        }
                    )
            # Do NOT add a synthetic finding for non-zero exit alone --
            # many tools exit non-zero on warnings. Let the LLM repairer
            # interpret raw output if needed.
            if not findings and rc not in (0, 1):
                # rc=1 is normal for bandit/gosec "findings exist" exit code
                findings.append(
                    {
                        "sev": "LOW",
                        "text": f"security scanner exited with code {rc}",
                        "file": "",
                        "line": "0",
                        "rule": "scanner-exit",
                    }
                )

        # Classify by severity: HIGH/CRITICAL->error, MEDIUM->warning, LOW->hint
        high_count = 0
        med_count = 0
        for f in findings:
            sev = f["sev"]
            if sev in ("HIGH", "CRITICAL"):
                severity, high_count = "error", high_count + 1
            elif sev == "MEDIUM":
                severity, med_count = "warning", med_count + 1
            else:
                severity = "hint"  # LOW -- don't block the gate
            loc = f" [{f['file']}:{f['line']}]" if f["file"] else ""
            result.issues.append(
                Issue(
                    file=f["file"],
                    line=int(f["line"] or 0),
                    severity=severity,
                    message=f"{f['text']}{loc}",
                    rule=f.get("rule", "security"),
                )
            )

        result.score = max(0.0, round(1.0 - high_count * 0.3 - med_count * 0.1, 3))
        result.passed = high_count == 0
        return result


# ── TrackableValidator ───────────────────────────────────────────────


class TrackableValidator(_ValidatorBase):
    """Validates the *Trackable* pillar: file naming, test structure, commit conventions."""

    _CONVENTIONAL_RE = re.compile(
        r"^(feat|fix|build|chore|ci|docs|style|refactor|perf|test)" r"(\([a-zA-Z0-9_./-]+\))?!?: .+$"
    )

    def name(self) -> str:
        return PRINCIPLE_TRACKABLE

    def validate(self) -> PrincipleResult:
        result = PrincipleResult(name=self.name(), passed=True, score=1.0)
        checks, score = 3.0, 0.0
        source_files = _find_source_files(self._root, self._profile.extensions, self._profile.skip_dirs)

        bad_names = [f for f in source_files if " " in os.path.basename(f)]
        if bad_names:
            for f in bad_names[:5]:
                result.issues.append(
                    Issue(
                        file=f,
                        severity="warning",
                        message="filename contains spaces",
                        rule="naming-convention",
                    )
                )
            score += max(0.0, 1.0 - len(bad_names) * 0.2)
        else:
            score += 1.0

        tp = re.compile(r"(test_|_test\.|\.test\.|spec_|_spec\.)", re.IGNORECASE)
        test_files = [f for f in source_files if tp.search(os.path.basename(f))]
        non_test = [f for f in source_files if not tp.search(os.path.basename(f))]
        if non_test and test_files:
            score += 1.0
        elif non_test and not test_files:
            result.issues.append(
                Issue(
                    severity="warning",
                    message="no test files found alongside source files",
                    rule="test-structure",
                )
            )
        else:
            score += 1.0

        rc, out = _run_command(("git", "log", "-1", "--format=%s"), self._root)
        if rc == 0 and out.strip():
            if self._CONVENTIONAL_RE.match(out.strip()):
                score += 1.0
            else:
                result.issues.append(
                    Issue(
                        severity="warning",
                        message="last commit does not follow Conventional Commits format",
                        rule="conventional-commits",
                    )
                )
        else:
            score += 0.5

        result.score = round(score / checks, 3)
        result.passed = len(bad_names) == 0 and (not non_test or len(test_files) > 0)
        return result


# ── ProjectCompletenessValidator ──────────────────────────────────────


class ProjectCompletenessValidator(_ValidatorBase):
    """Validates project structure: required files exist, no garbled files."""

    _GARBLED_FILE_RE = re.compile(r"^=[0-9]")

    def name(self) -> str:
        return PRINCIPLE_COMPLETENESS

    def validate(self) -> PrincipleResult:
        result = PrincipleResult(name=self.name(), passed=True, score=1.0)
        issues_count = 0
        checks = max(1, len(self._profile.required_project_files) + 1)
        score = 0.0

        # Check 1: Required project files exist
        # For files that are also in manifest_files (e.g. pyproject.toml for Python),
        # accept any alternative manifest as a valid substitute (e.g. requirements.txt).
        manifest_set = set(self._profile.manifest_files)
        has_any_manifest = any(os.path.exists(os.path.join(self._root, m)) for m in self._profile.manifest_files)
        for req_file in self._profile.required_project_files:
            full = os.path.join(self._root, req_file)
            if os.path.exists(full):
                score += 1.0
            elif req_file in manifest_set and has_any_manifest:
                # Alternative manifest file exists (e.g. requirements.txt instead
                # of pyproject.toml) — accept as equivalent.
                score += 1.0
            else:
                issues_count += 1
                result.issues.append(
                    Issue(
                        severity="error",
                        message=f"required project file missing: {req_file}",
                        rule="required-file-missing",
                    )
                )

        # Check 2: No garbled files in project root (artifacts from shell redirect bugs)
        garbled_count = 0
        try:
            for entry in os.scandir(self._root):
                if entry.is_file() and self._GARBLED_FILE_RE.match(entry.name):
                    garbled_count += 1
                    result.issues.append(
                        Issue(
                            file=entry.name,
                            severity="error",
                            message=f"garbled file detected (likely shell redirect artifact): {entry.name}",
                            rule="garbled-file",
                        )
                    )
        except OSError:
            logger.debug("Failed to scan directory for garbled files", exc_info=True)

        if garbled_count == 0:
            score += 1.0
        else:
            issues_count += garbled_count

        result.score = round(score / checks, 3)
        result.passed = issues_count == 0
        return result


# ── TrustGate orchestrator ───────────────────────────────────────────


class TrustGate:
    """Orchestrates all TRUST 5 validators and produces a weighted QualityReport.

    Runs validators concurrently via a thread pool and aggregates results
    using the configured pillar weights.
    """

    def __init__(self, config: QualityConfig, profile: LanguageProfile, project_root: str):
        self.config = config
        self._validators = [
            TestedValidator(project_root, profile, config),
            ReadableValidator(project_root, profile, config),
            UnderstandableValidator(project_root, profile, config),
            SecuredValidator(project_root, profile, config),
            TrackableValidator(project_root, profile, config),
            ProjectCompletenessValidator(project_root, profile, config),
        ]

    def validate(self) -> QualityReport:
        results: dict[str, PrincipleResult] = {}
        emit(M.QVAL, f"Running {len(self._validators)} validators concurrently...")

        def _run_one(v: _ValidatorBase) -> tuple[str, PrincipleResult]:
            try:
                return v.name(), v.validate()
            except (OSError, ValueError, RuntimeError) as e:  # validator: IO/parse errors
                logger.warning("Validator %s crashed: %s", v.name(), e)
                return v.name(), PrincipleResult(
                    name=v.name(),
                    passed=False,
                    score=0.0,
                    issues=[Issue(severity="error", message=str(e), rule="validator-crash")],
                )

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_run_one, v): v for v in self._validators}
            for future in as_completed(futures):
                vname, pr = future.result()
                results[vname] = pr
                status = "PASS" if pr.passed else "FAIL"
                emit(
                    M.QVAL,
                    f"  [{status}] {vname}: {pr.score:.3f} ({len(pr.issues)} issues)",
                )

        return self._build_report(results)

    def _build_report(self, results: dict[str, PrincipleResult]) -> QualityReport:
        total_score, total_errors, total_warnings, coverage_pct = 0.0, 0, 0, -1.0
        for pname in ALL_PRINCIPLES:
            pr = results.get(pname, PrincipleResult(name=pname))
            total_score += pr.score * PRINCIPLE_WEIGHTS.get(pname, 0.0)
            for issue in pr.issues:
                if issue.severity == "error":
                    total_errors += 1
                elif issue.severity == "warning":
                    total_warnings += 1
                if issue.rule == "coverage-measured":
                    try:
                        coverage_pct = float(issue.message.split("=")[1].rstrip("%"))
                    except (IndexError, ValueError):
                        logger.debug("Failed to parse coverage from %r", issue.message)
        score = round(total_score, 3)
        # Completeness is a pass/fail gate (weight=0), not a scored pillar.
        # If completeness fails, the report fails regardless of the score.
        completeness_pr = results.get(PRINCIPLE_COMPLETENESS)
        completeness_failed = completeness_pr is not None and not completeness_pr.passed
        return QualityReport(
            passed=score >= self.config.pass_score_threshold and total_errors == 0 and not completeness_failed,
            score=score,
            principles=results,
            total_errors=total_errors,
            total_warnings=total_warnings,
            coverage_pct=coverage_pct,
            timestamp=datetime.now(UTC).isoformat(),
        )
