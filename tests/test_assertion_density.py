from __future__ import annotations
import os
import textwrap
from trust5.core.quality import (
    _check_generic_assertions,
    _check_python_assertions,
    _has_python_assertions,
    check_assertion_density,
)

def _write_file(tmp_path: str, name: str, content: str) -> str:
    path = os.path.join(tmp_path, name)
    with open(path, "w") as f:
        f.write(textwrap.dedent(content))
    return path

def test_has_assertions_assert_stmt(tmp_path):
    """assert statement is detected."""
    import ast

    tree = ast.parse("def test_foo():\n    assert 1 == 1\n")
    func = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
    assert _has_python_assertions(func) is True
