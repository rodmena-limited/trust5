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

def test_has_assertions_self_assert(tmp_path):
    """self.assertEqual() is detected."""
    import ast

    tree = ast.parse("def test_foo(self):\n    self.assertEqual(1, 1)\n")
    func = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
    assert _has_python_assertions(func) is True

def test_has_assertions_pytest_raises(tmp_path):
    """pytest.raises() context manager is detected."""
    import ast

    source = "def test_foo():\n    with pytest.raises(ValueError):\n        pass\n"
    tree = ast.parse(source)
    func = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
    assert _has_python_assertions(func) is True

def test_no_assertions_vacuous(tmp_path):
    """Function with no assertions is flagged."""
    import ast

    tree = ast.parse("def test_foo():\n    x = 1 + 2\n    print(x)\n")
    func = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
    assert _has_python_assertions(func) is False

def test_no_assertions_empty(tmp_path):
    """Function with only pass is flagged."""
    import ast

    tree = ast.parse("def test_foo():\n    pass\n")
    func = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
    assert _has_python_assertions(func) is False

def test_python_all_good(tmp_path):
    """All test functions have assertions — density 1.0."""
    _write_file(
        tmp_path,
        "test_good.py",
        """\
        def test_add():
            assert 1 + 1 == 2

        def test_sub():
            assert 3 - 1 == 2
        """,
    )
    density, issues = _check_python_assertions([os.path.join(tmp_path, "test_good.py")])
    assert density == 1.0
    assert len(issues) == 0

def test_python_vacuous_tests(tmp_path):
    """One vacuous test function — density 0.5."""
    _write_file(
        tmp_path,
        "test_mixed.py",
        """\
        def test_good():
            assert True

        def test_bad():
            x = 1 + 2
            print(x)
        """,
    )
    density, issues = _check_python_assertions([os.path.join(tmp_path, "test_mixed.py")])
    assert density == 0.5
    assert len(issues) == 1
    assert "test_bad" in issues[0].message
    assert issues[0].rule == "vacuous-test"

def test_python_no_test_functions(tmp_path):
    """File with no test functions returns 1.0 (nothing to check)."""
    _write_file(
        tmp_path,
        "test_empty.py",
        """\
        def helper():
            return 42
        """,
    )
    density, issues = _check_python_assertions([os.path.join(tmp_path, "test_empty.py")])
    assert density == 1.0
    assert len(issues) == 0

def test_python_all_vacuous(tmp_path):
    """All tests vacuous — density 0.0."""
    _write_file(
        tmp_path,
        "test_bad.py",
        """\
        def test_a():
            pass

        def test_b():
            x = 1
        """,
    )
    density, issues = _check_python_assertions([os.path.join(tmp_path, "test_bad.py")])
    assert density == 0.0
    assert len(issues) == 2

def test_python_syntax_error_skipped(tmp_path):
    """Files with syntax errors are skipped, not crashed."""
    _write_file(tmp_path, "test_broken.py", "def test_x(:\n    assert True\n")
    density, issues = _check_python_assertions([os.path.join(tmp_path, "test_broken.py")])
    assert density == 1.0  # no tests parsed → 1.0

def test_generic_go_good(tmp_path):
    """Go test file with assertions scores well."""
    _write_file(
        tmp_path,
        "calc_test.go",
        """\
        func TestAdd(t *testing.T) {
            assert.Equal(t, 2, Add(1, 1))
        }
        func TestSub(t *testing.T) {
            assert.Equal(t, 1, Sub(2, 1))
        }
        """,
    )
    density, issues = _check_generic_assertions([os.path.join(tmp_path, "calc_test.go")], "go")
    assert density == 1.0
    assert len(issues) == 0
