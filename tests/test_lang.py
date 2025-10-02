from __future__ import annotations
from trust5.core.lang import (
    PROFILES,
    _detect_by_extensions,
    detect_language,
    get_profile,
)

def test_detect_language_python_pyproject(tmp_path):
    """Detects Python from pyproject.toml."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'foo'\n")
    assert detect_language(str(tmp_path)) == "python"

def test_detect_language_go_mod(tmp_path):
    """Detects Go from go.mod."""
    (tmp_path / "go.mod").write_text("module example.com/foo\ngo 1.21\n")
    assert detect_language(str(tmp_path)) == "go"

def test_detect_language_rust_cargo(tmp_path):
    """Detects Rust from Cargo.toml."""
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'foo'\n")
    assert detect_language(str(tmp_path)) == "rust"

def test_detect_language_typescript(tmp_path):
    """Detects TypeScript from tsconfig.json."""
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "tsconfig.json").write_text("{}")
    assert detect_language(str(tmp_path)) == "typescript"

def test_detect_language_falls_back_to_extensions(tmp_path):
    """When no manifest files exist, falls back to extension scanning."""
    # Create Python source files without any manifest
    (tmp_path / "main.py").write_text("print('hello')")
    (tmp_path / "utils.py").write_text("pass")
    assert detect_language(str(tmp_path)) == "python"

def test_detect_language_empty_dir(tmp_path):
    """Empty directory returns 'unknown'."""
    assert detect_language(str(tmp_path)) == "unknown"
