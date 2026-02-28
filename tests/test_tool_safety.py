"""Tests for tool safety limits — file size, glob result count, and read_files limits.

Covers audit issues:
  C3: Read tool has no file size limit
  H5: Glob has unbounded recursion
  H6: ReadFiles has no limits
"""

import os
import tempfile
from unittest.mock import patch

import pytest

from trust5.core.constants import (
    MAX_GLOB_RESULTS,
    MAX_READ_FILE_SIZE,
    MAX_READFILES_COUNT,
    MAX_READFILES_FILE_SIZE,
)
from trust5.core.tools import Tools

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _suppress_emit():
    """Prevent real event emission during tests."""
    with (
        patch("trust5.core.tools.emit") as mock_emit,
        patch("trust5.core.tools.emit_block") as mock_emit_block,
    ):
        yield mock_emit, mock_emit_block


@pytest.fixture()
def tools():
    """Unconstrained Tools instance (no owned_files restriction)."""
    return Tools()


@pytest.fixture(autouse=True)
def _reset_non_interactive():
    """Ensure the class-level _non_interactive flag is reset between tests."""
    original = Tools._non_interactive
    yield
    Tools._non_interactive = original


# ---------------------------------------------------------------------------
# read_file — file size limit (C3)
# ---------------------------------------------------------------------------


class TestReadFileSizeLimit:
    """C3: read_file must refuse files larger than MAX_READ_FILE_SIZE."""

    def test_read_file_rejects_oversized_file(self, tmp_path: object) -> None:
        """Files > 1 MB should return an error string, not content."""
        big_file = tmp_path / "big.txt"  # type: ignore[operator]
        big_file.write_bytes(b"x" * (MAX_READ_FILE_SIZE + 1))

        result = Tools.read_file(str(big_file))

        assert "too large" in result
        assert f"{MAX_READ_FILE_SIZE:,}" in result
        assert "offset/limit" in result.lower()

    def test_read_file_allows_file_at_limit(self, tmp_path: object) -> None:
        """Files exactly at the limit should be readable."""
        ok_file = tmp_path / "ok.txt"  # type: ignore[operator]
        ok_file.write_bytes(b"y" * MAX_READ_FILE_SIZE)

        result = Tools.read_file(str(ok_file))

        assert "too large" not in result
        assert "Error" not in result

    def test_read_file_allows_small_file(self, tmp_path: object) -> None:
        """Normal small files should work as before."""
        small_file = tmp_path / "small.txt"  # type: ignore[operator]
        small_file.write_text("hello world")

        result = Tools.read_file(str(small_file))

        assert result == "hello world"

    def test_read_file_with_offset_bypasses_size_limit(self, tmp_path: object) -> None:
        """When offset/limit are provided, the size check should NOT apply."""
        big_file = tmp_path / "big_offset.txt"  # type: ignore[operator]
        lines = [f"line {i}\n" for i in range(200_000)]
        big_file.write_text("".join(lines))
        # Ensure the file is actually > 1MB
        assert big_file.stat().st_size > MAX_READ_FILE_SIZE

        result = Tools.read_file(str(big_file), offset=1, limit=10)

        assert "too large" not in result
        assert "line 0" in result

    def test_read_file_with_limit_only_bypasses_size_limit(self, tmp_path: object) -> None:
        """Providing only limit= should also bypass the size check."""
        big_file = tmp_path / "big_limit.txt"  # type: ignore[operator]
        lines = [f"row {i}\n" for i in range(200_000)]
        big_file.write_text("".join(lines))
        assert big_file.stat().st_size > MAX_READ_FILE_SIZE

        result = Tools.read_file(str(big_file), limit=5)

        assert "too large" not in result
        assert "row 0" in result

    def test_read_file_error_includes_actual_size(self, tmp_path: object) -> None:
        """Error message should include the actual file size."""
        big_file = tmp_path / "big_info.txt"  # type: ignore[operator]
        size = MAX_READ_FILE_SIZE + 42
        big_file.write_bytes(b"z" * size)

        result = Tools.read_file(str(big_file))

        assert f"{size:,}" in result


# ---------------------------------------------------------------------------
# list_files — glob result limit (H5)
# ---------------------------------------------------------------------------


class TestGlobResultLimit:
    """H5: list_files must truncate results exceeding MAX_GLOB_RESULTS."""

    def test_list_files_truncates_excess_results(self, tmp_path: object) -> None:
        """Results exceeding MAX_GLOB_RESULTS should be truncated."""
        count = MAX_GLOB_RESULTS + 50
        for i in range(count):
            (tmp_path / f"file_{i:05d}.txt").write_text("")  # type: ignore[operator]

        results = Tools.list_files("*.txt", workdir=str(tmp_path))

        assert len(results) == MAX_GLOB_RESULTS

    def test_list_files_no_truncation_below_limit(self, tmp_path: object) -> None:
        """Results below the limit should not be truncated."""
        for i in range(5):
            (tmp_path / f"f{i}.txt").write_text("")  # type: ignore[operator]

        results = Tools.list_files("*.txt", workdir=str(tmp_path))

        assert len(results) == 5

    def test_list_files_logs_warning_on_truncation(self, tmp_path: object) -> None:
        """Truncation should produce a logger.warning call."""
        count = MAX_GLOB_RESULTS + 10
        for i in range(count):
            (tmp_path / f"w_{i:05d}.txt").write_text("")  # type: ignore[operator]

        with patch("trust5.core.tools.logger") as mock_logger:
            Tools.list_files("*.txt", workdir=str(tmp_path))
            mock_logger.warning.assert_called_once()
            warn_msg = mock_logger.warning.call_args[0][0]
            assert "truncated" in warn_msg.lower()


# ---------------------------------------------------------------------------
# read_files — count and per-file size limits (H6)
# ---------------------------------------------------------------------------


class TestReadFilesLimits:
    """H6: read_files must enforce count and per-file size limits."""

    def test_read_files_rejects_too_many_paths(self, tmp_path: object) -> None:
        """More than MAX_READFILES_COUNT paths should be truncated with a warning."""
        import json

        paths = []
        for i in range(MAX_READFILES_COUNT + 20):
            fp = tmp_path / f"rf_{i:05d}.txt"  # type: ignore[operator]
            fp.write_text(f"content {i}")
            paths.append(str(fp))

        result = Tools.read_files(paths)
        parsed = json.loads(result)

        # Should have at most MAX_READFILES_COUNT file entries + possible warning key
        file_keys = [k for k in parsed if not k.startswith("__")]
        assert len(file_keys) <= MAX_READFILES_COUNT

    def test_read_files_rejects_oversized_individual_file(self, tmp_path: object) -> None:
        """Individual files > MAX_READFILES_FILE_SIZE should get an error, not content."""
        import json

        big = tmp_path / "big_rf.txt"  # type: ignore[operator]
        big.write_bytes(b"x" * (MAX_READFILES_FILE_SIZE + 1))
        small = tmp_path / "small_rf.txt"  # type: ignore[operator]
        small.write_text("ok")

        result = Tools.read_files([str(big), str(small)])
        parsed = json.loads(result)

        assert "too large" in parsed[str(big)].lower()
        assert parsed[str(small)] == "ok"

    def test_read_files_allows_within_limits(self, tmp_path: object) -> None:
        """Normal usage within limits should work as before."""
        import json

        files = []
        for i in range(3):
            fp = tmp_path / f"ok_{i}.txt"  # type: ignore[operator]
            fp.write_text(f"data {i}")
            files.append(str(fp))

        result = Tools.read_files(files)
        parsed = json.loads(result)

        assert len(parsed) == 3
        for fp in files:
            assert "data" in parsed[fp]
