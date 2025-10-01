from __future__ import annotations
from unittest.mock import MagicMock, patch
from stabilize.models.status import WorkflowStatus
from trust5.tasks.validate_task import (
    MAX_REIMPLEMENTATIONS,
    ValidateTask,
    _build_test_env,
    _count_tests,
    _derive_module_test_files,
    _discover_test_files,
    _filter_test_file_lint,
    _parse_command,
    _scope_lint_command,
    _strip_nonexistent_files,
)
_PYTHON_PROFILE = {
    "language": "python",
    "extensions": (".py",),
    "test_command": ("python3", "-m", "pytest", "-v", "--tb=long", "-x"),
    "test_verify_command": 'Bash("pytest -v --tb=short")',
    "syntax_check_command": ("python3", "-m", "compileall", "-q", "."),
    "lint_check_commands": ("python3 -m ruff check --output-format=concise .",),
    "skip_dirs": ("__pycache__", ".venv", "venv", ".moai", ".trust5"),
}

def make_stage(context: dict | None = None) -> MagicMock:
    stage = MagicMock()
    stage.context = context or {}
    stage.context.setdefault("project_root", "/tmp/fake-project")
    stage.context.setdefault("language_profile", _PYTHON_PROFILE)
    return stage

def _subprocess_ok(*args, **kwargs):
    """subprocess.run mock returning success for all commands."""
    cmd = args[0] if args else kwargs.get("args", [])
    cmd_str = " ".join(cmd)
    result = MagicMock()
    result.returncode = 0
    result.stderr = ""
    if "pytest" in cmd_str:
        result.stdout = "3 passed in 0.5s"
    else:
        result.stdout = ""
    return result

def _subprocess_syntax_fail(*args, **kwargs):
    """subprocess.run mock returning syntax failure on first call, then OK."""
    cmd = args[0] if args else kwargs.get("args", [])
    cmd_str = " ".join(cmd)
    if "compileall" in cmd_str:
        result = MagicMock()
        result.returncode = 1
        result.stdout = "SyntaxError in main.py"
        result.stderr = ""
        return result
    # Lint and tests pass (but syntax fails first, so these shouldn't be reached)
    result = MagicMock()
    result.returncode = 0
    result.stdout = "3 passed" if "pytest" in cmd_str else ""
    result.stderr = ""
    return result

def _subprocess_test_fail(*args, **kwargs):
    """subprocess.run mock: syntax OK, lint OK, tests fail."""
    cmd = args[0] if args else kwargs.get("args", [])
    cmd_str = " ".join(cmd)
    if "compileall" in cmd_str:
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result
    if "ruff" in cmd_str:
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result
    # Tests fail
    result = MagicMock()
    result.returncode = 1
    result.stdout = "FAILED test_foo.py::test_bar - AssertionError\n1 failed, 2 passed"
    result.stderr = ""
    return result

def _subprocess_lint_fail(*args, **kwargs):
    """subprocess.run mock: syntax OK, lint FAILS."""
    cmd = args[0] if args else kwargs.get("args", [])
    cmd_str = " ".join(cmd)
    if "compileall" in cmd_str:
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result
    if "ruff" in cmd_str:
        result = MagicMock()
        result.returncode = 1
        result.stdout = "monte_carlo.py:72:9: F841 Local variable `alpha` is assigned to but never used"
        result.stderr = ""
        return result
    # Tests pass (but we shouldn't reach here because lint fails first)
    result = MagicMock()
    result.returncode = 0
    result.stdout = "3 passed"
    result.stderr = ""
    return result

def test_validate_all_pass(mock_run, mock_emit_block, mock_emit):
    """When syntax and tests both pass, return TaskResult.success with tests_passed=True."""
    task = ValidateTask()
    stage = make_stage()

    result = task.execute(stage)

    assert result.status == WorkflowStatus.SUCCEEDED
    assert result.outputs["tests_passed"] is True
    assert result.outputs["total_tests"] == 3

def test_validate_syntax_failure_jumps_to_repair(mock_run, mock_emit_block, mock_emit):
    """When syntax check fails, jump_to('repair') with failure_type='syntax'."""
    task = ValidateTask()
    stage = make_stage({"project_root": "/tmp/proj", "repair_attempt": 0})

    result = task.execute(stage)

    assert result.status == WorkflowStatus.REDIRECT
    assert result.target_stage_ref_id == "repair"
    assert result.context["failure_type"] == "syntax"
    assert result.context["_repair_requested"] is True

def test_validate_test_failure_jumps_to_repair(mock_run, mock_emit_block, mock_emit):
    """When tests fail, jump_to('repair') with failure_type='test'."""
    task = ValidateTask()
    stage = make_stage({"project_root": "/tmp/proj", "repair_attempt": 0})

    result = task.execute(stage)

    assert result.status == WorkflowStatus.REDIRECT
    assert result.target_stage_ref_id == "repair"
    assert result.context["failure_type"] == "test"
    assert result.context["tests_passed"] is False

def test_validate_max_attempts_reimplements(mock_run, mock_emit_block, mock_emit):
    """At max repair attempts, jump_to('implement') for reimplementation."""
    task = ValidateTask()
    # repair_attempt=5 >= max_attempts=5 triggers reimplementation
    stage = make_stage(
        {
            "project_root": "/tmp/proj",
            "repair_attempt": 5,
            "reimplementation_count": 0,
        }
    )

    result = task.execute(stage)

    assert result.status == WorkflowStatus.REDIRECT
    assert result.target_stage_ref_id == "implement"
    assert result.context["reimplementation_count"] == 1

def test_validate_all_reimplementations_exhausted(mock_run, mock_emit_block, mock_emit):
    """When all reimplementation attempts exhausted, return TaskResult.terminal()."""
    task = ValidateTask()
    # repair_attempt=5 >= max_attempts=5 AND reimpl_count >= max_reimpl → terminal
    stage = make_stage(
        {
            "project_root": "/tmp/proj",
            "repair_attempt": 5,
            "reimplementation_count": MAX_REIMPLEMENTATIONS,
        }
    )

    result = task.execute(stage)

    assert result.status == WorkflowStatus.TERMINAL
    error_msg = result.context.get("error", "")
    assert "reimplementation" in error_msg.lower() or "failing" in error_msg.lower()

def test_validate_lint_failure_jumps_to_repair(mock_run, mock_emit_block, mock_emit):
    """When lint check fails, jump_to('repair') with failure_type='lint'."""
    task = ValidateTask()
    stage = make_stage({"project_root": "/tmp/proj", "repair_attempt": 0})

    result = task.execute(stage)

    assert result.status == WorkflowStatus.REDIRECT
    assert result.target_stage_ref_id == "repair"
    assert result.context["failure_type"] == "lint"
    assert result.context["_repair_requested"] is True
    assert "F841" in result.context["test_output"]

def test_validate_lint_pass_proceeds_to_tests(mock_run, mock_emit_block, mock_emit):
    """When lint passes, proceed to test execution."""
    task = ValidateTask()
    stage = make_stage({"project_root": "/tmp/proj"})

    result = task.execute(stage)

    # All three steps passed (syntax, lint, tests)
    assert result.status == WorkflowStatus.SUCCEEDED
    assert result.outputs["tests_passed"] is True

def test_check_lint_returns_none_when_no_commands():
    """_check_lint returns None when no lint commands are configured."""
    result = ValidateTask._check_lint("/tmp/proj", [])
    assert result is None

def test_check_lint_returns_errors_on_failure(mock_run):
    """_check_lint returns combined error output when commands fail."""
    mock_run.return_value = MagicMock(
        returncode=1,
        stdout="file.py:10: F841 unused variable",
        stderr="",
    )

    result = ValidateTask._check_lint("/tmp/proj", [("ruff", "check", ".")])

    assert result is not None
    assert "F841" in result
    assert "Lint check failed" in result

def test_check_lint_skips_missing_tool(mock_run):
    """_check_lint silently skips commands whose tool is not installed."""
    result = ValidateTask._check_lint("/tmp/proj", [("ruff", "check", ".")])
    assert result is None

def test_check_lint_returns_none_on_all_pass(mock_run):
    """_check_lint returns None when all lint commands pass."""
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

    result = ValidateTask._check_lint(
        "/tmp/proj",
        [("ruff", "check", "."), ("gofmt", "-l", ".")],
    )

    assert result is None
    assert mock_run.call_count == 2

def test_check_lint_combines_multiple_failures(mock_run):
    """_check_lint combines errors from multiple failing commands."""
    mock_run.return_value = MagicMock(
        returncode=1,
        stdout="error output",
        stderr="",
    )

    result = ValidateTask._check_lint(
        "/tmp/proj",
        [("ruff", "check", "."), ("mypy", ".")],
    )

    assert result is not None
    assert "ruff" in result
    assert "mypy" in result

def test_check_lint_skips_module_not_found(mock_run):
    """_check_lint treats 'No module named X' as tool-not-installed, not lint error.

    Regression: when ruff is not installed, 'python3 -m ruff check' exits 1 with
    'No module named ruff'. This was falsely treated as a lint error, triggering
    an infinite validate-repair loop (repair pre-flight runs tests which pass).
    """
    mock_run.return_value = MagicMock(
        returncode=1,
        stdout="",
        stderr="/opt/homebrew/opt/python@3.14/bin/python3.14: No module named ruff",
    )

    result = ValidateTask._check_lint("/tmp/proj", [("python3", "-m", "ruff", "check", ".")])
    assert result is None  # Should be treated as skipped, not failure

def test_validate_no_lint_commands_skips_lint(mock_run, mock_emit_block, mock_emit):
    """When profile has no lint_check_commands, lint step is skipped."""
    task = ValidateTask()
    profile_no_lint = {
        "language": "python",
        "extensions": (".py",),
        "test_command": ("python3", "-m", "pytest", "-v", "--tb=long", "-x"),
        "syntax_check_command": ("python3", "-m", "compileall", "-q", "."),
        "lint_check_commands": (),
        "skip_dirs": ("__pycache__",),
    }
    stage = make_stage({"project_root": "/tmp/proj", "language_profile": profile_no_lint})

    result = task.execute(stage)

    # Should still succeed — lint step is just skipped
    assert result.status == WorkflowStatus.SUCCEEDED

def test_count_tests_pytest_output():
    """Verify _count_tests parses standard pytest summary output."""
    output = "===== 5 passed, 2 failed in 1.23s ====="
    assert _count_tests(output) == 7

def test_count_tests_go_output():
    """Verify _count_tests parses Go test output."""
    output = "ok  \tgithub.com/foo/bar\t0.123s\nok  \tgithub.com/foo/baz\t0.456s"
    assert _count_tests(output) == 2

def test_count_tests_jest_output():
    """Verify _count_tests parses Jest output."""
    # Jest format: "Tests:  4 passed, 5 total" — only counts "passed"
    output = "Tests:  4 passed, 5 total"
    assert _count_tests(output) == 4

def test_count_tests_empty_output():
    """Empty output returns 0."""
    assert _count_tests("") == 0

def test_propagate_context_used(mock_propagate, mock_run, mock_emit_block, mock_emit):
    """Verify propagate_context is called during failure handling (not manual copy).

    We trigger the failure path to confirm propagate_context is invoked.
    """
    task = ValidateTask()
    stage = make_stage({"project_root": "/tmp/proj", "repair_attempt": 0})

    task.execute(stage)

    mock_propagate.assert_called()
    # propagate_context is called with (stage.context, repair_context)
    call_args = mock_propagate.call_args
    assert isinstance(call_args[0][0], dict)  # source context
    assert isinstance(call_args[0][1], dict)  # target context

def test_validate_uses_plan_test_command(mock_run, mock_emit_block, mock_emit):
    """When plan_config has a test_command, it is used instead of defaults."""
    task = ValidateTask()
    stage = make_stage(
        {
            "project_root": "/tmp/proj",
            "plan_config": {"test_command": "python -m pytest -v --cov"},
        }
    )

    task.execute(stage)

    # Check that subprocess.run was called with the plan test command
    calls = mock_run.call_args_list
    found_plan_cmd = any("--cov" in " ".join(str(a) for a in call.args[0]) for call in calls if call.args)
    assert found_plan_cmd, f"Expected plan_config test_command in subprocess calls: {calls}"

def test_validate_uses_plan_lint_command(mock_run, mock_emit_block, mock_emit):
    """When plan_config has a lint_command, it is used instead of profile defaults.

    Regression: plan_config lint_command was ignored, causing venv-dependent lint
    tools to be invoked via system Python (where they may not be installed).
    """
    task = ValidateTask()
    stage = make_stage(
        {
            "project_root": "/tmp/proj",
            "plan_config": {"lint_command": "source venv/bin/activate && ruff check ."},
        }
    )

    task.execute(stage)

    # Check that subprocess.run was called with the plan lint command (wrapped in sh -c)
    calls = mock_run.call_args_list
    found_lint_cmd = any(
        "ruff check" in str(call.args[0]) for call in calls if call.args
    )
    assert found_lint_cmd, f"Expected plan_config lint_command in subprocess calls: {calls}"

def test_discover_test_files(tmp_path):
    """_discover_test_files finds test files matching standard patterns."""
    # Create test files
    (tmp_path / "test_foo.py").write_text("pass")
    (tmp_path / "bar_test.py").write_text("pass")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_baz.py").write_text("pass")
    # Create non-test files
    (tmp_path / "main.py").write_text("pass")
    (tmp_path / "utils.py").write_text("pass")
    # Create a skip directory
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "test_cached.py").write_text("pass")

    result = _discover_test_files(str(tmp_path))

    assert "test_foo.py" in result
    assert "bar_test.py" in result
    assert any("test_baz.py" in f for f in result)
    assert "main.py" not in result
    assert "utils.py" not in result
    # __pycache__ should be skipped
    assert not any("cached" in f for f in result)

def test_discover_test_files_empty(tmp_path):
    """Returns empty list when no test files found."""
    (tmp_path / "main.py").write_text("pass")
    result = _discover_test_files(str(tmp_path))
    assert result == []

def test_discover_test_files_respects_extensions(tmp_path):
    """Only finds test files matching given extensions."""
    (tmp_path / "test_foo.py").write_text("pass")
    (tmp_path / "test_bar.go").write_text("pass")

    py_only = _discover_test_files(str(tmp_path), extensions=(".py",))
    assert "test_foo.py" in py_only
    assert "test_bar.go" not in py_only

    go_only = _discover_test_files(str(tmp_path), extensions=(".go",))
    assert "test_bar.go" in go_only
    assert "test_foo.py" not in go_only

def test_validate_auto_detects_test_files(mock_discover, mock_run, mock_emit_block, mock_emit):
    """In serial pipeline (no test_files in context), auto-detect and inject them."""
    task = ValidateTask()
    stage = make_stage({"project_root": "/tmp/proj"})
    # No test_files in context initially
    assert "test_files" not in stage.context

    task.execute(stage)

    # After execution, test_files should be injected into context
    assert stage.context["test_files"] == ["test_foo.py", "test_bar.py"]
    mock_discover.assert_called_once()

def test_validate_skips_detection_when_test_files_present(mock_discover, mock_run, mock_emit_block, mock_emit):
    """When test_files already in context (parallel pipeline), skip discovery."""
    task = ValidateTask()
    stage = make_stage(
        {
            "project_root": "/tmp/proj",
            "test_files": ["tests/test_existing.py"],
        }
    )

    task.execute(stage)

    mock_discover.assert_not_called()

def test_test_files_propagated_to_repair_jump(mock_run, mock_emit_block, mock_emit):
    """test_files from context are carried into the repair jump context."""
    task = ValidateTask()
    stage = make_stage(
        {
            "project_root": "/tmp/proj",
            "repair_attempt": 0,
            "test_files": ["tests/test_core.py", "tests/test_utils.py"],
        }
    )

    result = task.execute(stage)

    assert result.status == WorkflowStatus.REDIRECT
    assert result.target_stage_ref_id == "repair"
    # test_files must be in the jump context (via propagate_context)
    assert result.context.get("test_files") == ["tests/test_core.py", "tests/test_utils.py"]

def test_test_files_propagated_to_reimpl_jump(mock_run, mock_emit_block, mock_emit):
    """test_files are carried to the reimplementation jump context."""
    task = ValidateTask()
    stage = make_stage(
        {
            "project_root": "/tmp/proj",
            "repair_attempt": 5,  # >= max_attempts
            "reimplementation_count": 0,
            "test_files": ["test_core.py"],
        }
    )

    result = task.execute(stage)

    assert result.status == WorkflowStatus.REDIRECT
    assert result.target_stage_ref_id == "implement"
    assert result.context.get("test_files") == ["test_core.py"]

def test_vfal_includes_module_name(mock_run, mock_emit_block, mock_emit):
    """VFAL emission includes [module_name] when present in context."""
    task = ValidateTask()
    stage = make_stage(
        {
            "project_root": "/tmp/proj",
            "repair_attempt": 0,
            "module_name": "core",
        }
    )

    task.execute(stage)

    # Find the VFAL emit call
    from trust5.core.message import M

    vfal_calls = [call for call in mock_emit.call_args_list if call.args and call.args[0] == M.VFAL]
    assert vfal_calls, "Expected at least one VFAL emission"
    assert "[core]" in vfal_calls[0].args[1]

def test_build_test_env_detects_src_dir(tmp_path):
    """_build_test_env adds src/ to PYTHONPATH when it exists."""
    (tmp_path / "src").mkdir()
    profile = {"source_roots": ("src",), "path_env_var": "PYTHONPATH"}

    env = _build_test_env(str(tmp_path), profile)

    assert env is not None
    assert str(tmp_path / "src") in env["PYTHONPATH"]

def test_build_test_env_returns_none_when_no_src(tmp_path):
    """_build_test_env returns None when no source root directory exists."""
    profile = {"source_roots": ("src", "lib"), "path_env_var": "PYTHONPATH"}

    env = _build_test_env(str(tmp_path), profile)

    assert env is None

def test_build_test_env_returns_none_when_no_profile():
    """_build_test_env returns None when profile has no source_roots."""
    env = _build_test_env("/tmp/proj", {})

    assert env is None

def test_build_test_env_preserves_existing_path(tmp_path, monkeypatch):
    """_build_test_env prepends source root to existing PYTHONPATH."""
    (tmp_path / "src").mkdir()
    monkeypatch.setenv("PYTHONPATH", "/existing/path")
    profile = {"source_roots": ("src",), "path_env_var": "PYTHONPATH"}

    env = _build_test_env(str(tmp_path), profile)

    assert env is not None
    assert env["PYTHONPATH"].startswith(str(tmp_path / "src"))
    assert "/existing/path" in env["PYTHONPATH"]

def test_build_test_env_tries_roots_in_order(tmp_path):
    """_build_test_env checks source_roots in order, uses first match."""
    # Only lib/ exists, not src/
    (tmp_path / "lib").mkdir()
    profile = {"source_roots": ("src", "lib"), "path_env_var": "PYTHONPATH"}

    env = _build_test_env(str(tmp_path), profile)

    assert env is not None
    assert str(tmp_path / "lib") in env["PYTHONPATH"]

def test_validate_passes_env_to_subprocess(mock_build_env, mock_run, mock_emit_block, mock_emit):
    """ValidateTask passes the env dict from _build_test_env to subprocess.run."""
    task = ValidateTask()
    stage = make_stage({"project_root": "/tmp/proj"})

    task.execute(stage)

    # subprocess.run should have been called with env kwarg
    for call in mock_run.call_args_list:
        assert call.kwargs.get("env") == {"PYTHONPATH": "/tmp/proj/src"}

def test_max_jumps_propagated_to_repair_jump(mock_run, mock_emit_block, mock_emit):
    """_max_jumps and _jump_count survive propagation into the repair jump context."""
    task = ValidateTask()
    stage = make_stage(
        {
            "project_root": "/tmp/proj",
            "repair_attempt": 0,
            "_max_jumps": 50,
            "_jump_count": 3,
        }
    )

    result = task.execute(stage)

    assert result.status == WorkflowStatus.REDIRECT
    assert result.target_stage_ref_id == "repair"
    assert result.context.get("_max_jumps") == 50
    # _jump_count incremented from 3 → 4 before the jump
    assert result.context.get("_jump_count") == 4

def test_max_jumps_propagated_to_reimpl_jump(mock_run, mock_emit_block, mock_emit):
    """_max_jumps and _jump_count survive propagation into the reimplementation jump context."""
    task = ValidateTask()
    stage = make_stage(
        {
            "project_root": "/tmp/proj",
            "repair_attempt": 5,
            "reimplementation_count": 0,
            "_max_jumps": 50,
            "_jump_count": 10,
        }
    )

    result = task.execute(stage)

    assert result.status == WorkflowStatus.REDIRECT
    assert result.target_stage_ref_id == "implement"
    assert result.context.get("_max_jumps") == 50
    # _jump_count incremented from 10 → 11 before the jump
    assert result.context.get("_jump_count") == 11

def test_repair_attempt_incremented_in_repair_jump(mock_run, mock_emit_block, mock_emit):
    """repair_attempt must be incremented (not overwritten by propagate_context).

    Regression test: propagate_context used to overwrite repair_attempt with the
    stale value from stage.context AFTER the increment was applied, causing the
    counter to never advance and the validate/repair loop to run indefinitely.
    """
    task = ValidateTask()
    # Simulate second validate→repair cycle: repair_attempt=2 in context
    stage = make_stage(
        {
            "project_root": "/tmp/proj",
            "repair_attempt": 2,
        }
    )

    result = task.execute(stage)

    assert result.status == WorkflowStatus.REDIRECT
    assert result.target_stage_ref_id == "repair"
    # Must be 3 (= 2 + 1), NOT 2 (stale propagation bug)
    assert result.context["repair_attempt"] == 3

def test_reimpl_resets_repair_attempt_to_zero(mock_run, mock_emit_block, mock_emit):
    """When reimplementing, repair_attempt must be reset to 0, not stale value.

    Regression test: propagate_context used to overwrite the explicit 0 with
    the old repair_attempt (e.g. 5), so reimplementation entered repair with
    attempt=5 and immediately re-triggered reimplementation again.
    """
    task = ValidateTask()
    stage = make_stage(
        {
            "project_root": "/tmp/proj",
            "repair_attempt": 5,  # >= max_attempts → triggers reimpl
            "reimplementation_count": 0,
        }
    )

    result = task.execute(stage)

    assert result.status == WorkflowStatus.REDIRECT
    assert result.target_stage_ref_id == "implement"
    # Must be 0, NOT 5 (stale propagation bug)
    assert result.context["repair_attempt"] == 0
    assert result.context["reimplementation_count"] == 1

def test_jump_limit_terminates_pipeline(mock_emit_block, mock_emit):
    """When _jump_count >= _max_jumps, validate returns TERMINAL instead of jumping."""
    task = ValidateTask()
    stage = make_stage(
        {
            "project_root": "/tmp/proj",
            "repair_attempt": 0,
            "_max_jumps": 20,
            "_jump_count": 20,  # at the limit
        }
    )

    result = task.execute(stage)

    assert result.status == WorkflowStatus.TERMINAL

def test_derive_module_test_files_matches_by_basename():
    """Derives test files from owned source file basenames."""
    all_tests = [
        "tests/test_engine.py",
        "tests/test_distributions.py",
        "tests/test_utils.py",
    ]
    owned = ["src/engine.py"]

    result = _derive_module_test_files(all_tests, owned)

    assert result == ["tests/test_engine.py"]

def test_derive_module_test_files_multiple_owned():
    """Matches test files for multiple owned source files."""
    all_tests = [
        "tests/test_engine.py",
        "tests/test_config.py",
        "tests/test_utils.py",
    ]
    owned = ["src/engine.py", "src/config.py"]

    result = _derive_module_test_files(all_tests, owned)

    assert "tests/test_engine.py" in result
    assert "tests/test_config.py" in result
    assert "tests/test_utils.py" not in result

def test_derive_module_test_files_substring_match():
    """Matches test files where owned basename is a substring of test core name."""
    all_tests = [
        "tests/test_simulation_engine.py",
        "tests/test_parser.py",
    ]
    owned = ["engine.py"]

    result = _derive_module_test_files(all_tests, owned)

    assert result == ["tests/test_simulation_engine.py"]

def test_derive_module_test_files_no_match():
    """Returns empty list when no test files match owned files."""
    all_tests = ["tests/test_auth.py", "tests/test_db.py"]
    owned = ["engine.py"]

    result = _derive_module_test_files(all_tests, owned)

    assert result == []

def test_derive_module_test_files_ignores_init():
    """__init__.py is excluded from basename matching."""
    all_tests = ["tests/test_init.py", "tests/test_engine.py"]
    owned = ["src/__init__.py", "src/engine.py"]

    result = _derive_module_test_files(all_tests, owned)

    assert result == ["tests/test_engine.py"]

def test_derive_module_test_files_suffix_pattern():
    """Handles _test suffix pattern (e.g., Go-style engine_test.go)."""
    all_tests = ["engine_test.go", "parser_test.go"]
    owned = ["engine.go"]

    result = _derive_module_test_files(all_tests, owned)

    assert result == ["engine_test.go"]

def test_validate_scopes_test_files_in_parallel_pipeline(
    mock_discover,
    mock_run,
    mock_emit_block,
    mock_emit,
):
    """In parallel pipeline (owned_files set, no test_files), scope to module tests only."""
    task = ValidateTask()
    stage = make_stage(
        {
            "project_root": "/tmp/proj",
            "owned_files": ["src/engine.py"],
            # No test_files — should derive from owned_files
        }
    )

    task.execute(stage)

    # Should only inject the engine test, not distributions
    assert stage.context["test_files"] == ["tests/test_engine.py"]

def test_parse_command_simple():
    """Simple command with no shell metacharacters uses shlex.split."""
    assert _parse_command("pytest -v --tb=short") == ("pytest", "-v", "--tb=short")

def test_parse_command_shell_and():
    """Command with && is wrapped in sh -c."""
    cmd = ". venv/bin/activate && pytest"
    result = _parse_command(cmd)
    assert result == ("sh", "-c", cmd)

def test_parse_command_pipe():
    """Command with | is wrapped in sh -c."""
    cmd = "pytest | tee output.log"
    result = _parse_command(cmd)
    assert result == ("sh", "-c", cmd)

def test_parse_command_semicolon():
    """Command with ; is wrapped in sh -c."""
    cmd = "cd src; pytest"
    result = _parse_command(cmd)
    assert result == ("sh", "-c", cmd)

def test_parse_command_dollar_var():
    """Command with $ variable is wrapped in sh -c."""
    cmd = "$HOME/bin/pytest"
    result = _parse_command(cmd)
    assert result == ("sh", "-c", cmd)

def test_parse_command_redirect():
    """Command with > redirect is wrapped in sh -c."""
    cmd = "pytest > output.txt"
    result = _parse_command(cmd)
    assert result == ("sh", "-c", cmd)

def test_parse_command_source_dot():
    """Command starting with '. ' (bash source) is wrapped in sh -c."""
    cmd = ". venv/bin/activate"
    result = _parse_command(cmd)
    assert result == ("sh", "-c", cmd)

def test_parse_command_source_dot_with_leading_whitespace():
    """'. ' detection works even with leading whitespace."""
    cmd = "  . venv/bin/activate && pytest"
    result = _parse_command(cmd)
    assert result == ("sh", "-c", cmd)

def test_parse_command_quoted_args():
    """Quoted arguments are properly handled by shlex."""
    assert _parse_command('pytest "tests/test foo.py"') == ("pytest", "tests/test foo.py")

def test_parse_command_backtick():
    """Backtick (command substitution) triggers sh -c wrapping."""
    cmd = "echo `date`"
    result = _parse_command(cmd)
    assert result == ("sh", "-c", cmd)

def test_parse_command_env_var_prefix():
    """VAR=value prefix triggers sh -c wrapping (not treated as binary name)."""
    cmd = "PYTHONPATH=src venv/bin/python -m pytest tests/ -v"
    result = _parse_command(cmd)
    assert result == ("sh", "-c", cmd)

def test_parse_command_env_var_prefix_multiple():
    """Multiple VAR=value prefixes also trigger sh -c."""
    cmd = "FOO=bar BAZ=qux python test.py"
    result = _parse_command(cmd)
    assert result == ("sh", "-c", cmd)

def test_validate_redetects_unknown_language(mock_detect, mock_run, mock_emit_block, mock_emit):
    """When language_profile says 'unknown' but detect_language finds python, update profile."""

    task = ValidateTask()
    unknown_profile = {
        "language": "unknown",
        "extensions": (),
        "test_command": ("echo", "no default test command"),
        "test_verify_command": "echo 'no tests'",
        "syntax_check_command": None,
        "skip_dirs": (".moai", ".trust5", ".git"),
    }
    stage = make_stage(
        {
            "project_root": "/tmp/proj",
            "language_profile": unknown_profile,
        }
    )

    task.execute(stage)

    # Profile should have been updated to python
    updated_profile = stage.context["language_profile"]
    assert updated_profile["language"] == "python"

def test_filter_test_file_lint_strips_test_lines():
    """Lint errors in test files are removed; source file errors are kept."""
    raw = (
        "tests/test_core.py:1:1: F401 [*] `os` imported but unused\n"
        "tests/test_core.py:2:1: F401 [*] `sys` imported but unused\n"
        "src/core.py:5:1: E302 expected 2 blank lines, got 1\n"
        "Found 3 errors."
    )
    result = _filter_test_file_lint(raw)
    assert "tests/test_core.py" not in result
    assert "src/core.py:5:1: E302" in result

def test_filter_test_file_lint_all_test_errors_returns_empty():
    """When ALL lint errors are in test files, return empty string (clean)."""
    raw = (
        "tests/test_foo.py:10:1: F401 `os` imported but unused\n"
        "test_bar.py:3:1: E302 expected 2 blank lines\n"
        "Found 2 errors."
    )
    result = _filter_test_file_lint(raw)
    assert result == ""

def test_filter_test_file_lint_no_test_errors_unchanged():
    """When no lint errors are in test files, output is preserved."""
    raw = (
        "src/engine.py:12:1: F401 `os` imported but unused\n"
        "src/utils.py:8:5: E302 expected 2 blank lines\n"
        "Found 2 errors."
    )
    result = _filter_test_file_lint(raw)
    assert "src/engine.py:12:1: F401" in result
    assert "src/utils.py:8:5: E302" in result

def test_filter_test_file_lint_tests_dir_pattern():
    """Lines with paths under tests/ directory are filtered."""
    raw = "tests/unit/test_calc.py:1:1: W291 trailing whitespace"
    assert _filter_test_file_lint(raw) == ""

def test_filter_test_file_lint_owned_files_scoping():
    """In parallel pipeline, only errors in owned files are kept."""
    raw = (
        "src/config.py:1:1: F401 `os` imported but unused\n"
        "src/statistics.py:5:1: F401 `math` imported but unused\n"
        "Found 2 errors."
    )
    # Only config.py is owned by this module
    result = _filter_test_file_lint(raw, owned_files=["src/config.py"])
    assert "src/config.py" in result
    assert "src/statistics.py" not in result

def test_filter_test_file_lint_owned_files_all_unowned():
    """When all errors are in unowned files, return empty."""
    raw = "src/other.py:3:1: E302 expected 2 blank lines"
    result = _filter_test_file_lint(raw, owned_files=["src/mine.py"])
    assert result == ""

def test_check_lint_filters_test_file_errors(mock_detect, mock_run, mock_emit_block, mock_emit):
    """_check_lint returns None when all lint errors are in test files."""
    lint_output = (
        "tests/test_core.py:1:1: F401 `os` imported but unused\n"
        "test_bar.py:3:1: E302 expected 2 blank lines\n"
        "Found 2 errors."
    )
    mock_run.return_value = MagicMock(returncode=1, stdout=lint_output, stderr="")

    result = ValidateTask._check_lint(
        "/tmp/proj",
        [("ruff", "check", ".")],
    )
    assert result is None

def test_scope_lint_command_removes_unowned_files():
    """Only files in owned_files are kept; others are dropped."""
    cmd = "source venv/bin/activate && python -m py_compile monte_carlo.py simulations.py stats.py"
    owned = ["monte_carlo.py"]

    result = _scope_lint_command(cmd, owned)

    assert "monte_carlo.py" in result
    assert "simulations.py" not in result
    assert "stats.py" not in result
    # Shell prefix preserved
    assert "source venv/bin/activate" in result

def test_scope_lint_command_preserves_directory_commands():
    """Directory-style commands (ruff check .) pass through unchanged."""
    cmd = "ruff check ."
    owned = ["monte_carlo.py"]

    result = _scope_lint_command(cmd, owned)

    assert result == cmd

def test_scope_lint_command_handles_shell_chain():
    """Shell chains with && are preserved; only file tokens are filtered."""
    cmd = "source venv/bin/activate && python -m py_compile a.py b.py"
    owned = ["a.py"]

    result = _scope_lint_command(cmd, owned)

    assert "source venv/bin/activate" in result
    assert "a.py" in result
    assert "b.py" not in result

def test_scope_lint_command_no_owned_returns_unchanged():
    """Empty owned_files list returns the command unchanged."""
    cmd = "python -m py_compile foo.py bar.py"

    result = _scope_lint_command(cmd, [])

    assert result == cmd

def test_scope_lint_command_all_files_owned():
    """When all file tokens are owned, command is unchanged."""
    cmd = "python -m py_compile monte_carlo.py stats.py"
    owned = ["monte_carlo.py", "stats.py"]

    result = _scope_lint_command(cmd, owned)

    assert "monte_carlo.py" in result
    assert "stats.py" in result

def test_scope_lint_command_none_owned_falls_back():
    """When no file tokens are owned, substitute owned basenames as fallback."""
    cmd = "python -m py_compile other.py another.py"
    owned = ["src/mine.py"]

    result = _scope_lint_command(cmd, owned)

    # Original unowned files should be gone
    assert "other.py" not in result
    assert "another.py" not in result
    # Owned basename should be used as fallback
    assert "mine.py" in result
    # Command prefix preserved
    assert "python -m py_compile" in result

def test_scope_lint_command_path_prefixed_files():
    """Files with path prefixes (src/foo.py) match against owned basenames."""
    cmd = "python -m py_compile src/engine.py src/other.py"
    owned = ["src/engine.py"]

    result = _scope_lint_command(cmd, owned)

    assert "src/engine.py" in result
    assert "src/other.py" not in result

def test_filter_lint_file_not_found_unowned():
    """FileNotFoundError lines for unowned files are filtered out."""
    raw = (
        "FileNotFoundError: [Errno 2] No such file or directory: 'simulations.py'\n"
        "src/engine.py:5:1: F401 unused import"
    )
    result = _filter_test_file_lint(raw, owned_files=["src/engine.py"])

    assert "simulations.py" not in result
    assert "src/engine.py:5:1: F401" in result

def test_filter_lint_file_not_found_owned():
    """FileNotFoundError lines for owned files are kept (real issues)."""
    raw = "FileNotFoundError: [Errno 2] No such file or directory: 'engine.py'"
    result = _filter_test_file_lint(raw, owned_files=["engine.py"])

    assert "engine.py" in result

def _subprocess_scoped_lint(*args, **kwargs):
    """subprocess.run mock that checks the lint command was scoped."""
    cmd = args[0] if args else kwargs.get("args", [])
    cmd_str = " ".join(cmd)
    result = MagicMock()
    result.returncode = 0
    result.stderr = ""
    if "pytest" in cmd_str:
        result.stdout = "3 passed in 0.5s"
    else:
        result.stdout = ""
    return result

def test_validate_scopes_lint_command_in_parallel_pipeline(mock_run, mock_emit_block, mock_emit):
    """In parallel pipeline with owned_files, plan lint command is scoped to owned files only."""
    task = ValidateTask()
    stage = make_stage(
        {
            "project_root": "/tmp/proj",
            "owned_files": ["monte_carlo.py"],
            "plan_config": {
                "lint_command": (
                    "source venv/bin/activate && python -m py_compile"
                    " monte_carlo.py simulations.py stats.py"
                ),
            },
        }
    )

    result = task.execute(stage)

    # Pipeline should succeed (all mocks return OK)
    assert result.status == WorkflowStatus.SUCCEEDED

    # Find the lint subprocess call (sh -c "...")
    lint_calls = [
        call for call in mock_run.call_args_list
        if call.args and "py_compile" in " ".join(str(a) for a in call.args[0])
    ]
    assert lint_calls, f"Expected a py_compile call in: {mock_run.call_args_list}"

    # The lint command should NOT contain unowned files
    lint_cmd_str = " ".join(str(a) for a in lint_calls[0].args[0])
    assert "monte_carlo.py" in lint_cmd_str
    assert "simulations.py" not in lint_cmd_str
    assert "stats.py" not in lint_cmd_str

def test_strip_nonexistent_files_removes_missing(tmp_path):
    """File tokens that don't exist on disk are removed."""
    (tmp_path / "exists.py").write_text("pass")
    # missing.py is NOT created
    cmd = "python -m py_compile exists.py missing.py"

    result = _strip_nonexistent_files(cmd, str(tmp_path))

    assert "exists.py" in result
    assert "missing.py" not in result

def test_strip_nonexistent_files_preserves_all_existing(tmp_path):
    """When all files exist, command is unchanged."""
    (tmp_path / "a.py").write_text("pass")
    (tmp_path / "b.py").write_text("pass")
    cmd = "python -m py_compile a.py b.py"

    result = _strip_nonexistent_files(cmd, str(tmp_path))

    assert "a.py" in result
    assert "b.py" in result

def test_strip_nonexistent_files_preserves_shell_chain(tmp_path):
    """Shell chains with && are preserved; only missing file tokens removed."""
    (tmp_path / "real.py").write_text("pass")
    cmd = "source venv/bin/activate && python -m py_compile real.py ghost.py"

    result = _strip_nonexistent_files(cmd, str(tmp_path))

    assert "source venv/bin/activate" in result
    assert "real.py" in result
    assert "ghost.py" not in result

def test_strip_nonexistent_files_directory_commands_unchanged(tmp_path):
    """Directory-style commands (no file tokens) pass through unchanged."""
    cmd = "ruff check ."

    result = _strip_nonexistent_files(cmd, str(tmp_path))

    assert result == cmd

def test_strip_nonexistent_files_all_missing_discovers_actual(tmp_path):
    """When ALL file tokens are missing, substitute actually-existing source files."""
    # Create actual source files in a subdirectory
    pkg = tmp_path / "monte_carlo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "simulator.py").write_text("pass")
    (pkg / "pi_estimation.py").write_text("pass")

    # Planner's stale lint command references files that don't exist
    cmd = "python -m py_compile monte_carlo.py examples/pi_estimation.py"

    result = _strip_nonexistent_files(cmd, str(tmp_path))

    # Should have substituted the actual files from disk
    assert "monte_carlo.py" not in result or "monte_carlo/" in result
    assert "simulator.py" in result or "pi_estimation.py" in result
    assert "python -m py_compile" in result

def test_strip_nonexistent_files_path_prefixed(tmp_path):
    """Files with path prefixes (src/foo.py) are checked relative to project root."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "engine.py").write_text("pass")
    # src/other.py does NOT exist

    cmd = "python -m py_compile src/engine.py src/other.py"

    result = _strip_nonexistent_files(cmd, str(tmp_path))

    assert "src/engine.py" in result
    assert "src/other.py" not in result
