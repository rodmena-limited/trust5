"""TRUST 5 quality gate framework for Correcto."""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from .config import QualityConfig
from .lang import LanguageProfile
from .message import M, emit

logger = logging.getLogger(__name__)

PRINCIPLE_TESTED = "tested"
PRINCIPLE_READABLE = "readable"
PRINCIPLE_UNDERSTANDABLE = "understandable"
PRINCIPLE_SECURED = "secured"
PRINCIPLE_TRACKABLE = "trackable"
PRINCIPLE_WEIGHTS: dict[str, float] = {
    PRINCIPLE_TESTED: 0.30,
    PRINCIPLE_READABLE: 0.15,
    PRINCIPLE_UNDERSTANDABLE: 0.15,
    PRINCIPLE_SECURED: 0.25,
    PRINCIPLE_TRACKABLE: 0.15,
}
ALL_PRINCIPLES = list(PRINCIPLE_WEIGHTS.keys())
PASS_SCORE_THRESHOLD = 0.70
SUBPROCESS_TIMEOUT = 120
MAX_FILE_LINES = 500  # fallback; prefer QualityConfig.max_file_lines

# Test file detection patterns — these are specification, not output.
# They are excluded from file-size checks since the repairer cannot modify them.
_TEST_PATTERN = re.compile(r"(test_|_test\.|\.test\.|spec_|_spec\.)", re.IGNORECASE)

_SKIP_SIZE_CHECK = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.lock",
        "go.sum",
        "poetry.lock",
        "Gemfile.lock",
        "composer.lock",
        "pubspec.lock",
    }
)


class Issue(BaseModel):
    file: str = ""
    line: int = 0
    severity: str = "error"
    message: str = ""
    rule: str = ""


class PrincipleResult(BaseModel):
    name: str
    passed: bool = False
    score: float = 0.0
    issues: list[Issue] = Field(default_factory=list)


class QualityReport(BaseModel):
    passed: bool = False
    score: float = 0.0
    principles: dict[str, PrincipleResult] = Field(default_factory=dict)
    total_errors: int = 0
    total_warnings: int = 0
    coverage_pct: float = -1.0
    timestamp: str = ""


def _run_command(cmd: tuple[str, ...] | None, cwd: str, timeout: int = SUBPROCESS_TIMEOUT) -> tuple[int, str]:
    if cmd is None:
        return 127, "no command configured"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)
        return proc.returncode, (proc.stdout + "\n" + proc.stderr).strip()
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"command timed out after {timeout}s"
    except Exception as e:
        return 1, str(e)


def _parse_coverage(output: str, language: str) -> float:
    patterns = {"python": r"TOTAL\s+\d+\s+\d+\s+(\d+)%", "go": r"coverage:\s+([\d.]+)%"}
    pat = patterns.get(language)
    if pat:
        m = re.search(pat, output)
        if m:
            return float(m.group(1))
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*%", output)
    return float(matches[-1]) if matches else -1.0


def _find_source_files(root: str, extensions: tuple[str, ...], skip_dirs: tuple[str, ...]) -> list[str]:
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        for fname in filenames:
            if any(fname.endswith(ext) for ext in extensions):
                files.append(os.path.join(dirpath, fname))
    return files


def _check_file_sizes(files: list[str], max_lines: int) -> list[Issue]:
    issues: list[Issue] = []
    for fpath in files:
        if os.path.basename(fpath) in _SKIP_SIZE_CHECK:
            continue
        try:
            with open(fpath, encoding="utf-8", errors="ignore") as f:
                count = sum(1 for _ in f)
            if count > max_lines:
                issues.append(
                    Issue(
                        file=fpath,
                        severity="warning",
                        message=f"file has {count} lines (max {max_lines})",
                        rule="file-size",
                    )
                )
        except OSError:
            pass
    return issues


def _check_doc_completeness(files: list[str], language: str) -> float:
    if not files:
        return 1.0
    doc_patterns = {
        "python": re.compile(r'^\s*("""|\'\'\')'),
        "go": re.compile(r"^//\s"),
        "rust": re.compile(r"^///"),
        "java": re.compile(r"^\s*/\*\*"),
    }
    pat = doc_patterns.get(language)
    if pat is None:
        return 1.0
    documented, total = 0, 0
    for fpath in files[:50]:
        total += 1
        try:
            with open(fpath, encoding="utf-8", errors="ignore") as f:
                head = f.read(2048)
            if pat.search(head):
                documented += 1
        except OSError:
            pass
    return documented / max(total, 1)


# ── Assertion density checking (Oracle Problem mitigation) ──────────

# Regex patterns to detect assertion statements per language.
# Used for generic (non-Python) file-level density heuristic.
_ASSERTION_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "go": (
        re.compile(r"\bt\.\w*(?:Error|Fatal|Fail)\w*\("),
        re.compile(r"\b(?:assert|require)\.\w+\("),
    ),
    "rust": (re.compile(r"\bassert(?:_eq|_ne)?!"),),
    "javascript": (re.compile(r"\bexpect\("), re.compile(r"\bassert[.(]")),
    "typescript": (re.compile(r"\bexpect\("), re.compile(r"\bassert[.(]")),
    "java": (re.compile(r"\bassert(?:Equals|True|False|NotNull|Null|That|Throws)\("),),
    "ruby": (re.compile(r"\bexpect\("), re.compile(r"\bassert(?:_equal|_nil|_match)?\b")),
    "kotlin": (re.compile(r"\bassert(?:Equals|True|False|NotNull|That)\("),),
    "swift": (re.compile(r"\bXCTAssert\w*\("),),
    "elixir": (re.compile(r"\bassert\b"),),
    "dart": (re.compile(r"\bexpect\("),),
    "php": (re.compile(r"\$this->assert\w+\("), re.compile(r"\bassert\w+\(")),
    "cpp": (re.compile(r"\b(?:ASSERT|EXPECT)_\w+\("),),
    "c": (re.compile(r"\b(?:ASSERT|CU_ASSERT|ck_assert)\w*\("),),
    "csharp": (re.compile(r"\bAssert\.\w+\("),),
    "scala": (re.compile(r"\bassert\b"),),
}

_TEST_FUNC_PATTERNS: dict[str, re.Pattern[str]] = {
    "go": re.compile(r"^func\s+Test\w+\s*\("),
    "rust": re.compile(r"^\s*fn\s+test_\w+"),
    "javascript": re.compile(r"^\s*(?:it|test)\s*\("),
    "typescript": re.compile(r"^\s*(?:it|test)\s*\("),
    "java": re.compile(r"^\s*@Test\b"),
    "ruby": re.compile(r"^\s*(?:it|test)\s+['\"]"),
    "kotlin": re.compile(r"^\s*@Test\b"),
    "swift": re.compile(r"^\s*func\s+test\w+\s*\("),
    "elixir": re.compile(r"^\s*test\s+"),
    "dart": re.compile(r"^\s*test\s*\("),
    "php": re.compile(r"^\s*(?:public\s+)?function\s+test\w+\s*\("),
    "cpp": re.compile(r"^\s*TEST(?:_F)?\s*\("),
    "c": re.compile(r"^\s*void\s+test_\w+\s*\("),
    "csharp": re.compile(r"^\s*\[(?:Test|Fact)\]"),
    "scala": re.compile(r"^\s*(?:it|test)\s*[(\"]"),
}


def _has_python_assertions(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True if a Python function AST contains assertion statements."""
    for node in ast.walk(func_node):
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr.startswith("assert"):
                return True
            if isinstance(fn, ast.Attribute) and fn.attr == "raises":
                return True
        if isinstance(node, ast.With):
            for item in node.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Attribute):
                    if ctx.func.attr == "raises":
                        return True
    return False


def _check_python_assertions(test_files: list[str]) -> tuple[float, list[Issue]]:
    """AST-based per-function assertion check for Python test files."""
    total_tests = 0
    tests_with_assertions = 0
    vacuous: list[tuple[str, str]] = []

    for fpath in test_files:
        try:
            with open(fpath, encoding="utf-8", errors="ignore") as f:
                source = f.read()
            tree = ast.parse(source, filename=fpath)
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            total_tests += 1
            if _has_python_assertions(node):
                tests_with_assertions += 1
            else:
                vacuous.append((fpath, node.name))

    if total_tests == 0:
        return 1.0, []
    density = tests_with_assertions / total_tests
    issues: list[Issue] = []
    for fpath, fname in vacuous[:10]:
        issues.append(
            Issue(
                file=fpath,
                severity="error",
                message=f"test function '{fname}' contains no assertions (vacuous test)",
                rule="vacuous-test",
            )
        )
    return density, issues


def _check_generic_assertions(test_files: list[str], language: str) -> tuple[float, list[Issue]]:
    """Regex-based file-level assertion density for non-Python languages."""
    assertion_pats = _ASSERTION_PATTERNS.get(language)
    test_func_pat = _TEST_FUNC_PATTERNS.get(language)
    if not assertion_pats or not test_func_pat:
        return 1.0, []

    total_tests = 0
    total_assertions = 0

    for fpath in test_files:
        try:
            with open(fpath, encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except OSError:
            continue
        lines = content.splitlines()
        total_tests += sum(1 for line in lines if test_func_pat.match(line))
        for pat in assertion_pats:
            total_assertions += len(pat.findall(content))

    if total_tests == 0:
        return 1.0, []
    density = min(1.0, total_assertions / total_tests)
    issues: list[Issue] = []
    if density < 0.5:
        issues.append(
            Issue(
                severity="error",
                message=f"test assertion density is {density:.0%} "
                f"({total_assertions} assertions across {total_tests} test functions)",
                rule="low-assertion-density",
            )
        )
    elif density < 1.0:
        issues.append(
            Issue(
                severity="warning",
                message=f"test assertion density is {density:.0%} "
                f"({total_assertions} assertions across {total_tests} test functions)",
                rule="low-assertion-density",
            )
        )
    return density, issues


def check_assertion_density(
    project_root: str,
    extensions: tuple[str, ...],
    skip_dirs: tuple[str, ...],
    language: str,
) -> tuple[float, list[Issue]]:
    """Check that test functions contain meaningful assertions.

    Returns (density_score, issues).
    1.0 = all tests have assertions, 0.0 = no tests have assertions.
    """
    all_files = _find_source_files(project_root, extensions, skip_dirs)
    test_files = [f for f in all_files if _TEST_PATTERN.search(os.path.basename(f))]
    if not test_files:
        return 1.0, []
    if language == "python":
        return _check_python_assertions(test_files)
    return _check_generic_assertions(test_files, language)


class _ValidatorBase:
    def __init__(self, project_root: str, profile: LanguageProfile, config: QualityConfig):
        self._root = project_root
        self._profile = profile
        self._config = config

    def name(self) -> str:
        raise NotImplementedError

    def validate(self) -> PrincipleResult:
        raise NotImplementedError


class TestedValidator(_ValidatorBase):
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
            self._root, self._profile.extensions, self._profile.skip_dirs, self._profile.language,
        )
        score += assertion_density
        result.issues.extend(assertion_issues)
        result.issues.append(
            Issue(severity="hint", message=f"assertion_density={assertion_density:.2f}", rule="assertion-density-measured")
        )

        result.score = round(score / checks, 3)
        result.passed = (
            rc_test == 0
            and type_errors == 0
            and (cov < 0 or cov >= self._config.coverage_threshold)
            and assertion_density >= 0.5
        )
        return result


_TOOL_MISSING_PATTERNS = (
    "no module named",
    "command not found",
    "not found in path",
    "is not recognized",
    "not installed",
    "cannot run program",
)


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

        for cmd_str in cmds:
            rc, out = _run_command(("sh", "-c", cmd_str), self._root)
            if rc == 0:
                continue
            if rc == 127 or _is_tool_missing(out):
                continue
            lint_failures += 1
            result.issues.append(
                Issue(
                    severity="error",
                    message=out[:2000],
                    rule="lint-raw",
                )
            )

        result.score = max(0.0, round(1.0 - lint_failures * 0.2, 3))
        result.passed = lint_failures == 0
        return result


def _is_tool_missing(output: str) -> bool:
    lower = output.lower()
    return any(pat in lower for pat in _TOOL_MISSING_PATTERNS)


class UnderstandableValidator(_ValidatorBase):
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

        doc_score = _check_doc_completeness(source_files, self._profile.language)
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


def _parse_security_json(out: str) -> list[dict[str, str]]:
    """Parse JSON security output (bandit, gosec). Returns list of findings."""
    try:
        data = json.loads(out.strip())
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    results = data.get("results", []) if isinstance(data, dict) else []
    findings: list[dict[str, str]] = []
    for r in results:
        findings.append(
            {
                "sev": str(r.get("issue_severity", "LOW")).upper(),
                "text": str(r.get("issue_text", "")),
                "file": str(r.get("filename", "")),
                "line": str(r.get("line_number", 0)),
                "rule": str(r.get("test_id", "")),
            }
        )
    return findings


def _filter_excluded_findings(
    findings: list[dict[str, str]],
    skip_dirs: tuple[str, ...],
) -> list[dict[str, str]]:
    """Remove findings whose file path falls inside a skipped directory.

    Defense-in-depth: security tools (bandit, gosec) have their own
    ``--exclude`` flags but behaviour is version-dependent and may miss
    directory variants (e.g. ``venv`` vs ``./venv``).  Filtering here
    guarantees that third-party / vendored code never poisons the score.
    """
    if not skip_dirs:
        return findings
    skip = set(skip_dirs)
    out: list[dict[str, str]] = []
    for f in findings:
        fpath = f.get("file", "")
        if fpath and _path_in_skip_dirs(fpath, skip):
            continue
        out.append(f)
    return out


def _path_in_skip_dirs(filepath: str, skip_dirs: set[str]) -> bool:
    """Return True if *filepath* is inside any of *skip_dirs*."""
    parts = os.path.normpath(filepath).split(os.sep)
    # Check every directory component (exclude the filename itself).
    return any(p in skip_dirs for p in parts[:-1])


class SecuredValidator(_ValidatorBase):
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

        # Parse findings — try JSON first, then minimal fallback
        findings = _parse_security_json(out)
        findings = _filter_excluded_findings(findings, self._profile.skip_dirs)
        if not findings and rc != 0:
            # Only flag genuine CVE references — avoid matching metric summaries
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
            # Do NOT add a synthetic finding for non-zero exit alone —
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

        # Classify by severity: HIGH/CRITICAL→error, MEDIUM→warning, LOW→hint
        high_count = 0
        med_count = 0
        for f in findings:
            sev = f["sev"]
            if sev in ("HIGH", "CRITICAL"):
                severity, high_count = "error", high_count + 1
            elif sev == "MEDIUM":
                severity, med_count = "warning", med_count + 1
            else:
                severity = "hint"  # LOW — don't block the gate
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


class TrackableValidator(_ValidatorBase):
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


class TrustGate:
    def __init__(self, config: QualityConfig, profile: LanguageProfile, project_root: str):
        self.config = config
        self._validators = [
            TestedValidator(project_root, profile, config),
            ReadableValidator(project_root, profile, config),
            UnderstandableValidator(project_root, profile, config),
            SecuredValidator(project_root, profile, config),
            TrackableValidator(project_root, profile, config),
        ]

    def validate(self) -> QualityReport:
        results: dict[str, PrincipleResult] = {}
        emit(M.QVAL, f"Running {len(self._validators)} validators concurrently...")

        def _run_one(v: _ValidatorBase) -> tuple[str, PrincipleResult]:
            try:
                return v.name(), v.validate()
            except Exception as e:
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
                        pass
        score = round(total_score, 3)
        return QualityReport(
            passed=score >= self.config.pass_score_threshold and total_errors == 0,
            score=score,
            principles=results,
            total_errors=total_errors,
            total_warnings=total_warnings,
            coverage_pct=coverage_pct,
            timestamp=datetime.now(UTC).isoformat(),
        )


def meets_quality_gate(report: QualityReport, config: QualityConfig) -> bool:
    if not report.passed or report.total_errors > config.max_errors:
        return False
    return not (report.coverage_pct >= 0 and report.coverage_pct < config.coverage_threshold)


def is_improved(prev: dict[str, Any] | None, curr: QualityReport) -> bool:
    if prev is None:
        return False
    return bool(curr.score > prev.get("score", 0.0) or curr.total_errors < prev.get("total_errors", 999))


def is_stagnant(prev: dict[str, Any] | None, curr: QualityReport) -> bool:
    if prev is None:
        return False
    return bool(
        curr.score == prev.get("score", -1)
        and curr.total_errors == prev.get("total_errors", -1)
        and curr.total_warnings == prev.get("total_warnings", -1)
    )
