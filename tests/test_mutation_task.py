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
