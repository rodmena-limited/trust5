"""Tests for trust5/core/context_builder.py — repair prompt building (Bug 13 fix)."""

from __future__ import annotations

import os
import tempfile

from trust5.core.context_builder import build_repair_prompt


def _make_source_file(tmpdir: str, name: str, content: str) -> None:
    """Create a source file in tmpdir."""
    path = os.path.join(tmpdir, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


# ── Acceptance criteria injection (Bug 13) ───────────────────────────


def test_repair_prompt_includes_acceptance_criteria():
    """build_repair_prompt includes acceptance criteria from plan_config."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _make_source_file(tmpdir, "app.py", "def hello(): pass\n")
        result = build_repair_prompt(
            test_output="FAILED test_hello - AssertionError",
            project_root=tmpdir,
            plan_config={
                "acceptance_criteria": [
                    "Must return greeting string",
                    "Must handle empty name",
                ],
            },
        )
        assert "ACCEPTANCE CRITERIA (what the code MUST do):" in result
        assert "AC-1. Must return greeting string" in result
        assert "AC-2. Must handle empty name" in result
        assert "do NOT work around the test" in result


def test_repair_prompt_no_criteria_when_empty():
    """No acceptance criteria section when plan_config has empty list."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _make_source_file(tmpdir, "app.py", "def hello(): pass\n")
        result = build_repair_prompt(
            test_output="FAILED",
            project_root=tmpdir,
            plan_config={"acceptance_criteria": []},
        )
        assert "ACCEPTANCE CRITERIA" not in result


def test_repair_prompt_no_criteria_when_no_plan_config():
    """No acceptance criteria section when plan_config is None."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _make_source_file(tmpdir, "app.py", "def hello(): pass\n")
        result = build_repair_prompt(
            test_output="FAILED",
            project_root=tmpdir,
            plan_config=None,
        )
        assert "ACCEPTANCE CRITERIA" not in result


def test_repair_prompt_criteria_before_repair_rules():
    """Acceptance criteria appears before REPAIR RULES section."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _make_source_file(tmpdir, "app.py", "def hello(): pass\n")
        result = build_repair_prompt(
            test_output="FAILED",
            project_root=tmpdir,
            plan_config={
                "acceptance_criteria": ["Must work"],
            },
        )
        criteria_pos = result.index("ACCEPTANCE CRITERIA")
        rules_pos = result.index("REPAIR RULES")
        assert criteria_pos < rules_pos, "Criteria must appear before REPAIR RULES"


# ── Spec context ─────────────────────────────────────────────────────


def test_repair_prompt_includes_spec_section():
    """Spec section appears when spec_id is provided and spec files exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _make_source_file(tmpdir, "app.py", "x = 1\n")
        spec_dir = os.path.join(tmpdir, ".trust5", "specs", "SPEC-001")
        os.makedirs(spec_dir)
        with open(os.path.join(spec_dir, "spec.md"), "w") as f:
            f.write("# Test Spec\nBuild a calculator.")
        result = build_repair_prompt(
            test_output="FAILED",
            project_root=tmpdir,
            spec_id="SPEC-001",
        )
        assert "SPEC CONTEXT:" in result
        assert "Build a calculator" in result


# ── Previous failures ────────────────────────────────────────────────


def test_repair_prompt_includes_previous_failures():
    """Previous repair attempt summaries are included."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _make_source_file(tmpdir, "app.py", "x = 1\n")
        result = build_repair_prompt(
            test_output="FAILED",
            project_root=tmpdir,
            previous_failures=["Attempt 1: changed import", "Attempt 2: fixed typo"],
        )
        assert "PREVIOUS REPAIR ATTEMPTS" in result
        assert "Attempt 1: changed import" in result
        assert "Attempt 2: fixed typo" in result


def test_repair_prompt_no_previous_section_when_empty():
    """No previous section when no failures."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _make_source_file(tmpdir, "app.py", "x = 1\n")
        result = build_repair_prompt(
            test_output="FAILED",
            project_root=tmpdir,
            previous_failures=[],
        )
        assert "PREVIOUS REPAIR ATTEMPTS" not in result


# ── Attempt number and basic structure ───────────────────────────────


def test_repair_prompt_shows_attempt_number():
    with tempfile.TemporaryDirectory() as tmpdir:
        _make_source_file(tmpdir, "app.py", "x = 1\n")
        result = build_repair_prompt(
            test_output="FAILED",
            project_root=tmpdir,
            attempt=3,
        )
        assert "REPAIR ATTEMPT 3" in result


def test_repair_prompt_has_working_directory():
    with tempfile.TemporaryDirectory() as tmpdir:
        _make_source_file(tmpdir, "app.py", "x = 1\n")
        result = build_repair_prompt(
            test_output="FAILED",
            project_root=tmpdir,
        )
        assert f"WORKING DIRECTORY: {tmpdir}" in result


def test_repair_prompt_warns_no_testbed():
    """Prompt explicitly warns against /testbed references."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _make_source_file(tmpdir, "app.py", "x = 1\n")
        result = build_repair_prompt(
            test_output="FAILED",
            project_root=tmpdir,
        )
        assert "/testbed does NOT exist" in result


def test_repair_prompt_has_repair_rules():
    with tempfile.TemporaryDirectory() as tmpdir:
        _make_source_file(tmpdir, "app.py", "x = 1\n")
        result = build_repair_prompt(
            test_output="FAILED",
            project_root=tmpdir,
        )
        assert "REPAIR RULES:" in result
        assert "NEVER modify test files" in result


# ── Plan test command override ───────────────────────────────────────


def test_repair_prompt_uses_plan_test_command():
    """When plan_config has test_command, it's used instead of profile default."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _make_source_file(tmpdir, "app.py", "x = 1\n")
        result = build_repair_prompt(
            test_output="FAILED",
            project_root=tmpdir,
            plan_config={"test_command": "pytest -v --tb=long"},
        )
        assert 'Bash("pytest -v --tb=long")' in result
