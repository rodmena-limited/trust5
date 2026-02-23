"""Tests for _exclude_test_files_from_lint_cmd() in trust5/tasks/validate_helpers.py."""

from __future__ import annotations

from trust5.tasks.validate_helpers import _exclude_test_files_from_lint_cmd, _filter_test_file_lint

# ── Python / ruff ────────────────────────────────────────────────────


def test_exclude_ruff_directory_cmd():
    """ruff check . gets --extend-exclude flag inserted before the trailing dot."""
    result = _exclude_test_files_from_lint_cmd("ruff check .", "python")
    assert "--extend-exclude" in result
    assert result.endswith(".")
    assert "test_*" in result


def test_exclude_ruff_already_has_exclude():
    """Idempotent: command that already has --exclude is returned unchanged."""
    cmd = "ruff check --exclude foo ."
    result = _exclude_test_files_from_lint_cmd(cmd, "python")
    assert result == cmd


def test_exclude_ruff_already_has_extend_exclude():
    """Idempotent: command that already has --extend-exclude is returned unchanged."""
    cmd = "ruff check --extend-exclude foo ."
    result = _exclude_test_files_from_lint_cmd(cmd, "python")
    assert result == cmd


def test_exclude_ruff_file_list_filters_test_files():
    """File-list mode: test files are removed from the argument list."""
    result = _exclude_test_files_from_lint_cmd("ruff check test_main.py main.py", "python")
    assert "test_main.py" not in result
    assert "main.py" in result


def test_exclude_ruff_file_list_keeps_non_test():
    """File-list mode: non-test files are kept unchanged."""
    cmd = "ruff check main.py utils.py"
    result = _exclude_test_files_from_lint_cmd(cmd, "python")
    assert result == cmd


def test_exclude_ruff_all_test_files_returns_unchanged():
    """If all file args are test files, return unchanged (don't create empty cmd)."""
    cmd = "ruff check test_main.py test_utils.py"
    result = _exclude_test_files_from_lint_cmd(cmd, "python")
    assert result == cmd


# ── TypeScript / eslint ──────────────────────────────────────────────


def test_exclude_eslint_directory_cmd():
    """npx eslint . gets --ignore-pattern flags injected."""
    result = _exclude_test_files_from_lint_cmd("npx eslint .", "typescript")
    assert "--ignore-pattern" in result
    assert result.endswith(".")


def test_exclude_eslint_already_has_ignore():
    """Idempotent: command that already has --ignore-pattern is unchanged."""
    cmd = "npx eslint --ignore-pattern 'foo' ."
    result = _exclude_test_files_from_lint_cmd(cmd, "typescript")
    assert result == cmd


def test_exclude_eslint_file_list():
    """File-list mode: test files are removed from eslint arguments."""
    result = _exclude_test_files_from_lint_cmd("npx eslint test_app.ts app.ts", "typescript")
    assert "test_app.ts" not in result
    assert "app.ts" in result


# ── Go ───────────────────────────────────────────────────────────────


def test_exclude_go_file_list():
    """Go file-list mode: _test.go files are removed."""
    result = _exclude_test_files_from_lint_cmd("gofmt -l main.go main_test.go", "go")
    assert "main_test.go" not in result
    assert "main.go" in result


def test_exclude_go_directory_no_flags():
    """Go has no exclude flags for directory commands — returned unchanged."""
    cmd = "gofmt -l ."
    result = _exclude_test_files_from_lint_cmd(cmd, "go")
    assert result == cmd


# ── Edge cases ───────────────────────────────────────────────────────


def test_exclude_empty_cmd():
    result = _exclude_test_files_from_lint_cmd("", "python")
    assert result == ""


def test_exclude_unknown_language():
    """Unknown language with directory target: no flags to add, unchanged."""
    cmd = "foocheck ."
    result = _exclude_test_files_from_lint_cmd(cmd, "unknown")
    assert result == cmd


def test_exclude_chained_commands():
    """Chained commands: only the ruff segment is modified."""
    result = _exclude_test_files_from_lint_cmd("ruff check . && mypy .", "python")
    parts = result.split(" && ")
    assert len(parts) == 2
    assert "--extend-exclude" in parts[0]
    assert "--extend-exclude" not in parts[1]


def test_exclude_spec_files_from_js():
    """File-list: _spec-pattern files are removed from the argument list."""
    result = _exclude_test_files_from_lint_cmd("eslint app.js app_spec.js", "javascript")
    assert "app_spec.js" not in result
    assert "app.js" in result


# ── _filter_test_file_lint rich format ────────────────────────────────


def test_filter_lint_concise_format():
    """Concise format (ruff --output-format=concise): test file lines are dropped."""
    raw = "tests/test_main.py:12:1: F401 `pytest` imported but unused\nmain.py:5:1: E302 expected 2 blank lines\n"
    result = _filter_test_file_lint(raw)
    assert "test_main.py" not in result
    assert "main.py" in result


def test_filter_lint_rich_format():
    """Rich format (ruff default): test file lines with ' --> ' prefix are dropped."""
    raw = (
        "F401 [*] `pytest` imported but unused\n"
        " --> tests/test_main.py:3:8\n"
        "  |\n"
        "3 | import pytest\n"
        "  |\n"
        "\n"
        "E302 Expected 2 blank lines, found 1\n"
        " --> main.py:5:1\n"
        "  |\n"
        "5 | def foo():\n"
        "  |\n"
    )
    result = _filter_test_file_lint(raw)
    assert "test_main.py" not in result
    assert "main.py" in result


def test_filter_lint_rich_format_only_test_files():
    """Rich format with only test file errors: all lint lines dropped."""
    raw = "F401 [*] `pytest` imported but unused\n --> tests/test_main.py:3:8\n  |\n3 | import pytest\n  |\n"
    result = _filter_test_file_lint(raw)
    # All lines reference test files, so the file-path line is dropped.
    # Non-file-matching lines (context lines) are kept by the filter.
    assert "test_main.py" not in result
