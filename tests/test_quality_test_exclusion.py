"""Tests for quality validator changes that exclude test files."""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch

from trust5.core.config import QualityConfig
from trust5.core.lang_profiles import LanguageProfile
from trust5.core.quality_models import _TEST_PATTERN
from trust5.core.quality_validators import ReadableValidator, SecuredValidator, UnderstandableValidator


def _make_profile(
    language: str = "python",
    extensions: tuple[str, ...] = (".py",),
    lint_commands: tuple[str, ...] = (),
    lint_check_commands: tuple[str, ...] = (),
    skip_dirs: tuple[str, ...] = (),
    security_command: tuple[str, ...] | None = None,
) -> LanguageProfile:
    return LanguageProfile(
        language=language,
        extensions=extensions,
        test_command=("pytest",),
        test_verify_command="pytest",
        lint_commands=lint_commands,
        lint_check_commands=lint_check_commands,
        syntax_check_command=None,
        package_install_prefix="pip install",
        lsp_language_id="python",
        skip_dirs=skip_dirs,
        manifest_files=("pyproject.toml",),
        prompt_hints="test",
        security_command=security_command,
    )


def _default_config() -> QualityConfig:
    return QualityConfig()


# ── _TEST_PATTERN tests ─────────────────────────────────────────────


def test_test_pattern_matches_test_prefix():
    assert _TEST_PATTERN.search("test_main.py")


def test_test_pattern_matches_test_suffix():
    assert _TEST_PATTERN.search("main_test.py")


def test_test_pattern_matches_dot_test():
    assert _TEST_PATTERN.search("main.test.js")


def test_test_pattern_matches_spec():
    assert _TEST_PATTERN.search("main_spec.ts")


def test_test_pattern_no_match_normal():
    assert not _TEST_PATTERN.search("main.py")


# ── SecuredValidator test file filtering ────────────────────────────


@patch("trust5.core.quality_validators._run_command")
@patch("trust5.core.quality_validators.emit")
def test_secured_filters_test_file_findings(_mock_emit, mock_run):
    """Security findings from test files are filtered out before scoring."""
    security_json = json.dumps(
        {
            "results": [
                {
                    "issue_severity": "HIGH",
                    "issue_text": "Use of assert",
                    "filename": "test_app.py",
                    "line_number": 10,
                    "test_id": "B101",
                },
                {
                    "issue_severity": "HIGH",
                    "issue_text": "Hardcoded password",
                    "filename": "app.py",
                    "line_number": 20,
                    "test_id": "B106",
                },
            ]
        }
    )
    mock_run.return_value = (1, security_json)

    with tempfile.TemporaryDirectory() as tmpdir:
        profile = _make_profile(security_command=("bandit", "-r", "."))
        validator = SecuredValidator(tmpdir, profile, _default_config())
        result = validator.validate()

    error_files = [i.file for i in result.issues if i.severity == "error"]
    assert "app.py" in error_files
    assert "test_app.py" not in error_files


# ── UnderstandableValidator test file exclusion ─────────────────────


@patch("trust5.core.quality_validators._run_command")
@patch("trust5.core.quality_validators.emit")
def test_understandable_uses_non_test_files_for_doc_check(_mock_emit, mock_run):
    """Doc completeness is computed only from non-test files."""
    mock_run.return_value = (0, "")

    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "main.py"), "w") as f:
            f.write('"""Module docstring."""\ndef main(): pass\n')
        with open(os.path.join(tmpdir, "test_main.py"), "w") as f:
            f.write("def test_x(): pass\n")
        with open(os.path.join(tmpdir, "test_utils.py"), "w") as f:
            f.write("def test_y(): pass\n")

        profile = _make_profile(lint_commands=("ruff check .",))
        validator = UnderstandableValidator(tmpdir, profile, _default_config())
        result = validator.validate()

    doc_issues = [i for i in result.issues if i.rule == "doc-completeness"]
    assert len(doc_issues) == 0


@patch("trust5.core.quality_validators._run_command")
@patch("trust5.core.quality_validators.emit")
def test_understandable_filters_test_file_warnings(_mock_emit, mock_run):
    """Warning count excludes lines mentioning test files."""
    lines = ["test_main.py:1:1: warning: unused import\n"] * 11
    lines.append("main.py:1:1: warning: unused variable\n")
    mock_run.return_value = (1, "".join(lines))

    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "main.py"), "w") as f:
            f.write('"""Doc."""\ndef main(): pass\n')

        profile = _make_profile(lint_commands=("ruff check .",))
        config = _default_config()  # max_warnings=10
        validator = UnderstandableValidator(tmpdir, profile, config)
        result = validator.validate()

    warn_issues = [i for i in result.issues if i.rule == "warnings-threshold"]
    assert len(warn_issues) == 0


# ── ReadableValidator test exclusion ────────────────────────────────


@patch("trust5.core.quality_validators._run_command")
@patch("trust5.core.quality_validators.emit")
def test_readable_excludes_test_files_from_lint(_mock_emit, mock_run):
    """Lint command is modified to exclude test files before execution."""
    mock_run.return_value = (0, "")

    with tempfile.TemporaryDirectory() as tmpdir:
        profile = _make_profile(lint_check_commands=("ruff check .",))
        validator = ReadableValidator(tmpdir, profile, _default_config())
        validator.validate()

    assert mock_run.called
    cmd_tuple = mock_run.call_args[0][0]
    actual_cmd = cmd_tuple[2]
    assert "--extend-exclude" in actual_cmd
