"""Tests for _ensure_ext in trust5/workflows/parallel_pipeline.py (Bug 10 fix)."""

from __future__ import annotations

from trust5.workflows.parallel_pipeline import _ensure_ext


def test_extensionless_path_gets_default():
    assert _ensure_ext("tests/test_task", ".py") == "tests/test_task.py"


def test_py_extension_preserved():
    assert _ensure_ext("src/app.py", ".py") == "src/app.py"


def test_toml_extension_preserved():
    assert _ensure_ext("pyproject.toml", ".py") == "pyproject.toml"


def test_json_extension_preserved():
    assert _ensure_ext("package.json", ".ts") == "package.json"


def test_yaml_extension_preserved():
    assert _ensure_ext("config.yaml", ".py") == "config.yaml"


def test_cfg_extension_preserved():
    assert _ensure_ext("setup.cfg", ".py") == "setup.cfg"


def test_go_mod_preserved():
    assert _ensure_ext("go.mod", ".go") == "go.mod"


def test_cargo_toml_preserved():
    assert _ensure_ext("Cargo.toml", ".rs") == "Cargo.toml"


def test_tsconfig_preserved():
    assert _ensure_ext("tsconfig.json", ".ts") == "tsconfig.json"


def test_dockerfile_preserved():
    assert _ensure_ext("Dockerfile.dev", ".py") == "Dockerfile.dev"


def test_nested_extensionless():
    assert _ensure_ext("src/handlers/auth", ".go") == "src/handlers/auth.go"


def test_dotfile_preserved():
    assert _ensure_ext(".gitignore", ".py") == ".gitignore"


def test_lock_file_preserved():
    assert _ensure_ext("poetry.lock", ".py") == "poetry.lock"


def test_empty_default_ext_noop():
    assert _ensure_ext("tests/test_task", "") == "tests/test_task"


def test_rs_extension_preserved():
    assert _ensure_ext("src/main.rs", ".rs") == "src/main.rs"
