import difflib
import glob
import json
import os
import re
import shlex
import subprocess
from typing import Any
from .init import ProjectInitializer
from .message import M, emit, emit_block
_BLOCKED_COMMAND_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+-[^\s]*r[^\s]*f", re.IGNORECASE),  # rm -rf, rm -fr, etc.
    re.compile(r"\brm\s+-[^\s]*f[^\s]*r", re.IGNORECASE),  # rm -fr variants
    re.compile(r"\bmkfs\b", re.IGNORECASE),
    re.compile(r"\bdd\s+", re.IGNORECASE),
    re.compile(r"\bchmod\s+777\b"),
    re.compile(r"\bchmod\s+-R\s+777\b"),
    re.compile(r">\s*/dev/sd[a-z]"),
    re.compile(r"\b:(){ :\|:& };:", re.IGNORECASE),  # fork bomb
    re.compile(r"\bcurl\b.*\|\s*(?:bash|sh|zsh)\b"),  # curl | bash
    re.compile(r"\bwget\b.*\|\s*(?:bash|sh|zsh)\b"),  # wget | bash
    re.compile(r"\bsqlite3\s+.*\.trust5/"),  # Accessing trust5 internal DB crashes pipeline
]
_SAFE_COMMAND_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bfind\b\s+.+-exec\s+rm\b"),  # find ... -exec rm ... is scoped
    re.compile(r"\bfind\b\s+.+-delete\b"),  # find ... -delete is scoped
]
_VALID_PACKAGE_RE = re.compile(r"^[a-zA-Z0-9._-]+[a-zA-Z0-9._\-\[\]>=<,! ]*$")
_TEST_FILE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(^|/)test_[^/]+$"),  # test_foo.py
    re.compile(r"(^|/)[^/]+_test\.[^/]+$"),  # foo_test.py, foo_test.go
    re.compile(r"(^|/)tests/"),  # tests/ directory
    re.compile(r"(^|/)spec/"),  # spec/ directory
    re.compile(r"(^|/)[^/]+_spec\.[^/]+$"),  # foo_spec.rb
]

def _matches_test_pattern(path: str) -> bool:
    """Check if a file path matches common test file patterns."""
    for pattern in _TEST_FILE_PATTERNS:
        if pattern.search(path):
            return True
    return False

class Tools:
    _non_interactive: bool = False
    def __init__(
        self,
        owned_files: list[str] | None = None,
        denied_files: list[str] | None = None,
        deny_test_patterns: bool = False,
    ) -> None:
        self._owned_files: set[str] | None = None
        if owned_files:
            self._owned_files = {os.path.realpath(f) for f in owned_files}
        self._denied_files: set[str] | None = None
        if denied_files:
            self._denied_files = {os.path.realpath(f) for f in denied_files}
        self._deny_test_patterns = deny_test_patterns

    def set_non_interactive(cls, value: bool = True) -> None:
        cls._non_interactive = value

    def is_non_interactive(cls) -> bool:
        return cls._non_interactive
