"""TRUST 5 quality gate data models, constants, and utility functions."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess

from pydantic import BaseModel, Field

# ── Principle names and weights ──────────────────────────────────────

PRINCIPLE_TESTED = "tested"
PRINCIPLE_READABLE = "readable"
PRINCIPLE_UNDERSTANDABLE = "understandable"
PRINCIPLE_SECURED = "secured"
PRINCIPLE_TRACKABLE = "trackable"
PRINCIPLE_COMPLETENESS = "completeness"
PRINCIPLE_WEIGHTS: dict[str, float] = {
    PRINCIPLE_TESTED: 0.30,
    PRINCIPLE_READABLE: 0.15,
    PRINCIPLE_UNDERSTANDABLE: 0.15,
    PRINCIPLE_SECURED: 0.25,
    PRINCIPLE_TRACKABLE: 0.15,
    PRINCIPLE_COMPLETENESS: 0.0,  # pass/fail gate, not a scored pillar
}
ALL_PRINCIPLES = list(PRINCIPLE_WEIGHTS.keys())
PASS_SCORE_THRESHOLD = 0.70
SUBPROCESS_TIMEOUT = 120
MAX_FILE_LINES = 500  # fallback; prefer QualityConfig.max_file_lines

# Test file detection patterns -- these are specification, not output.
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


# ── Data models ──────────────────────────────────────────────────────


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


# ── Subprocess helpers ───────────────────────────────────────────────


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


# ── File-system helpers ──────────────────────────────────────────────


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


# ── Tool-missing detection ───────────────────────────────────────────

_TOOL_MISSING_PATTERNS = (
    "no module named",
    "command not found",
    "not found in path",
    "is not recognized",
    "not installed",
    "cannot run program",
)


def _is_tool_missing(output: str) -> bool:
    lower = output.lower()
    return any(pat in lower for pat in _TOOL_MISSING_PATTERNS)


# ── Security output parsing ──────────────────────────────────────────


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
