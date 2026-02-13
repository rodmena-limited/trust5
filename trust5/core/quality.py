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
_TOOL_MISSING_PATTERNS = (
    "no module named",
    "command not found",
    "not found in path",
    "is not recognized",
    "not installed",
    "cannot run program",
)

class Issue(BaseModel):
    file: str = ''
    line: int = 0
    severity: str = 'error'
    message: str = ''
    rule: str = ''

class PrincipleResult(BaseModel):
    name: str
    passed: bool = False
    score: float = 0.0
    issues: list[Issue] = Field(default_factory=list)
