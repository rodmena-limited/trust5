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
