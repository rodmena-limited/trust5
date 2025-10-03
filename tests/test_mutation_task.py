from __future__ import annotations
import os
import textwrap
from unittest.mock import MagicMock, patch
from stabilize.models.status import WorkflowStatus
from trust5.tasks.mutation_task import (
    MutationTask,
    Mutant,
    _apply_mutant,
    _restore_file,
    generate_mutants,
)

def _write_file(directory: str, name: str, content: str) -> str:
    path = os.path.join(directory, name)
    with open(path, "w") as f:
        f.write(textwrap.dedent(content))
    return path

def test_generate_mutants_finds_operators(tmp_path):
    """Mutation candidates are found for comparison operators."""
    _write_file(
        tmp_path,
        "calc.py",
        """\
        def is_positive(x):
            return x > 0

        def is_equal(a, b):
            return a == b
        """,
    )
    mutants = generate_mutants([os.path.join(tmp_path, "calc.py")], max_mutants=100)
    assert len(mutants) >= 2
    descriptions = [m.description for m in mutants]
    assert any("gt" in d for d in descriptions)
    assert any("eq" in d for d in descriptions)

def test_generate_mutants_skips_comments(tmp_path):
    """Lines starting with # are skipped."""
    _write_file(
        tmp_path,
        "calc.py",
        """\
        # x == y should be checked
        def foo():
            return True
        """,
    )
    mutants = generate_mutants([os.path.join(tmp_path, "calc.py")], max_mutants=100)
    # The comment line should be skipped, only "True" in the function body
    comment_mutants = [m for m in mutants if m.line_no == 1]
    assert len(comment_mutants) == 0

def test_generate_mutants_respects_max(tmp_path):
    """max_mutants caps the output size."""
    _write_file(
        tmp_path,
        "calc.py",
        """\
        a = True
        b = False
        c = True
        d = False
        e = True
        f = False
        """,
    )
    mutants = generate_mutants([os.path.join(tmp_path, "calc.py")], max_mutants=3)
    assert len(mutants) <= 3

def test_generate_mutants_empty_files(tmp_path):
    """Empty source file list returns no mutants."""
    mutants = generate_mutants([], max_mutants=10)
    assert len(mutants) == 0

def test_generate_mutants_no_operators(tmp_path):
    """File with no mutable operators returns no mutants."""
    _write_file(tmp_path, "empty.py", "x = 42\ny = 'hello'\n")
    mutants = generate_mutants([os.path.join(tmp_path, "empty.py")], max_mutants=10)
    assert len(mutants) == 0

def test_apply_and_restore(tmp_path):
    """Applying a mutant modifies the file; restoring brings it back."""
    path = _write_file(tmp_path, "calc.py", "x = True\ny = False\n")
    mutant = Mutant(
        file=path,
        line_no=1,
        original_line="x = True\n",
        mutated_line="x = False\n",
        description="calc.py:1 (true→false)",
    )
    original_content = _apply_mutant(mutant)

    with open(path) as f:
        assert f.readline() == "x = False\n"

    _restore_file(path, original_content)

    with open(path) as f:
        assert f.readline() == "x = True\n"

def _make_stage(context: dict | None = None) -> MagicMock:
    stage = MagicMock()
    stage.context = context or {}
    stage.context.setdefault("project_root", "/tmp/fake")
    return stage

def test_mutation_no_source_files(mock_find, mock_emit):
    """No source files → skip with score -1.0."""
    task = MutationTask()
    stage = _make_stage({"language_profile": {"language": "python", "extensions": [".py"]}})

    with patch.object(task, "_build_profile") as mock_profile:
        mock_profile.return_value = MagicMock(
            extensions=(".py",),
            skip_dirs=("__pycache__",),
            test_command=("pytest",),
        )
        result = task.execute(stage)

    assert result.status == WorkflowStatus.SUCCEEDED
    assert result.outputs["mutation_score"] == -1.0

def test_mutation_no_mutants(mock_gen, mock_find, mock_emit):
    """No mutable operators → skip with score -1.0."""
    task = MutationTask()
    stage = _make_stage()

    with patch.object(task, "_build_profile") as mock_profile:
        mock_profile.return_value = MagicMock(
            extensions=(".py",),
            skip_dirs=("__pycache__",),
            test_command=("pytest",),
        )
        result = task.execute(stage)

    assert result.status == WorkflowStatus.SUCCEEDED
    assert result.outputs["mutation_score"] == -1.0

def test_mutation_all_killed(mock_gen, mock_run, mock_apply, mock_restore, mock_find, mock_emit):
    """All mutants killed → score 1.0, success."""
    mock_gen.return_value = [
        Mutant("/tmp/calc.py", 1, "x > 0\n", "x >= 0\n", "calc.py:1 (gt→gte)"),
        Mutant("/tmp/calc.py", 2, "a == b\n", "a != b\n", "calc.py:2 (eq→neq)"),
    ]
    mock_run.return_value = MagicMock(returncode=1)  # tests fail → mutant killed

    task = MutationTask()
    stage = _make_stage()

    with patch.object(task, "_build_profile") as mock_profile:
        mock_profile.return_value = MagicMock(
            extensions=(".py",),
            skip_dirs=("__pycache__",),
            test_command=("pytest",),
        )
        result = task.execute(stage)

    assert result.status == WorkflowStatus.SUCCEEDED
    assert result.outputs["mutation_score"] == 1.0
    assert result.outputs["mutants_killed"] == 2
    assert result.outputs["mutants_survived"] == 0

def test_mutation_some_survived(mock_gen, mock_run, mock_apply, mock_restore, mock_find, mock_emit):
    """Some mutants survive → failed_continue with score < 1.0."""
    mock_gen.return_value = [
        Mutant("/tmp/calc.py", 1, "x > 0\n", "x >= 0\n", "calc.py:1 (gt→gte)"),
        Mutant("/tmp/calc.py", 2, "a == b\n", "a != b\n", "calc.py:2 (eq→neq)"),
    ]
    # First mutant: tests pass (survived), second: tests fail (killed)
    mock_run.side_effect = [MagicMock(returncode=0), MagicMock(returncode=1)]

    task = MutationTask()
    stage = _make_stage()

    with patch.object(task, "_build_profile") as mock_profile:
        mock_profile.return_value = MagicMock(
            extensions=(".py",),
            skip_dirs=("__pycache__",),
            test_command=("pytest",),
        )
        result = task.execute(stage)

    assert result.status == WorkflowStatus.FAILED_CONTINUE
    assert result.outputs["mutation_score"] == 0.5
    assert result.outputs["mutants_survived"] == 1
    assert result.outputs["mutants_killed"] == 1

def test_mutation_timeout_counts_as_killed(mock_gen, mock_run, mock_apply, mock_restore, mock_find, mock_emit):
    """Timeout during test run counts as 'killed' (behaviour changed)."""
    import subprocess

    mock_gen.return_value = [
        Mutant("/tmp/calc.py", 1, "x > 0\n", "x >= 0\n", "calc.py:1"),
    ]
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="pytest", timeout=120)

    task = MutationTask()
    stage = _make_stage()

    with patch.object(task, "_build_profile") as mock_profile:
        mock_profile.return_value = MagicMock(
            extensions=(".py",),
            skip_dirs=("__pycache__",),
            test_command=("pytest",),
        )
        result = task.execute(stage)

    assert result.status == WorkflowStatus.SUCCEEDED
    assert result.outputs["mutants_killed"] == 1

def test_oracle_assertion_density_error():
    """Low assertion density triggers error in methodology validation."""
    from trust5.core.config import QualityConfig
    from trust5.core.quality_gates import MethodologyContext, _validate_oracle_mitigations

    ctx = MethodologyContext(assertion_density=0.3)
    config = QualityConfig()
    issues = _validate_oracle_mitigations(ctx, config)
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert "assertion density" in issues[0].message

def test_oracle_assertion_density_warning():
    """Medium assertion density triggers warning."""
    from trust5.core.config import QualityConfig
    from trust5.core.quality_gates import MethodologyContext, _validate_oracle_mitigations

    ctx = MethodologyContext(assertion_density=0.7)
    config = QualityConfig()
    issues = _validate_oracle_mitigations(ctx, config)
    assert len(issues) == 1
    assert issues[0].severity == "warning"

def test_oracle_assertion_density_pass():
    """Good assertion density generates no issues."""
    from trust5.core.config import QualityConfig
    from trust5.core.quality_gates import MethodologyContext, _validate_oracle_mitigations

    ctx = MethodologyContext(assertion_density=0.95)
    config = QualityConfig()
    issues = _validate_oracle_mitigations(ctx, config)
    assert len(issues) == 0
