"""Tests for trust5/core/lang.py — language detection and profiles."""

from __future__ import annotations

from trust5.core.lang import (
    PROFILES,
    _detect_by_extensions,
    detect_language,
    get_profile,
)

# ── detect_language (manifest-based) ──────────────────────────────────────


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


def test_detect_language_nonexistent_dir():
    """Nonexistent directory returns 'unknown' (doesn't crash)."""
    assert detect_language("/nonexistent/fake/dir/12345") == "unknown"


# ── _detect_by_extensions ─────────────────────────────────────────────────


def test_detect_by_extensions_python(tmp_path):
    """Counts .py files and returns python."""
    (tmp_path / "main.py").write_text("pass")
    (tmp_path / "lib.py").write_text("pass")
    assert _detect_by_extensions(str(tmp_path)) == "python"


def test_detect_by_extensions_go(tmp_path):
    """Counts .go files and returns go."""
    (tmp_path / "main.go").write_text("package main")
    (tmp_path / "util.go").write_text("package main")
    assert _detect_by_extensions(str(tmp_path)) == "go"


def test_detect_by_extensions_dominant_language(tmp_path):
    """When mixed extensions exist, the dominant language wins."""
    # 3 Python files, 1 Go file
    (tmp_path / "a.py").write_text("pass")
    (tmp_path / "b.py").write_text("pass")
    (tmp_path / "c.py").write_text("pass")
    (tmp_path / "main.go").write_text("package main")
    assert _detect_by_extensions(str(tmp_path)) == "python"


def test_detect_by_extensions_subdirectory(tmp_path):
    """Scans one level into subdirectories."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "engine.py").write_text("pass")
    (src / "utils.py").write_text("pass")
    assert _detect_by_extensions(str(tmp_path)) == "python"


def test_detect_by_extensions_skips_node_modules(tmp_path):
    """Skips node_modules directory."""
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "package.js").write_text("module.exports = {}")
    # Only a single Go file outside node_modules
    (tmp_path / "main.go").write_text("package main")
    assert _detect_by_extensions(str(tmp_path)) == "go"


def test_detect_by_extensions_skips_hidden_dirs(tmp_path):
    """Skips directories starting with '.' (e.g. .git, .venv)."""
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / "pip.py").write_text("pass")
    # Only Rust file visible
    (tmp_path / "main.rs").write_text("fn main() {}")
    assert _detect_by_extensions(str(tmp_path)) == "rust"


def test_detect_by_extensions_empty_dir(tmp_path):
    """Empty directory returns 'unknown'."""
    assert _detect_by_extensions(str(tmp_path)) == "unknown"


def test_detect_by_extensions_nonexistent_dir():
    """Nonexistent directory returns 'unknown' without raising."""
    assert _detect_by_extensions("/nonexistent/path/12345") == "unknown"


def test_detect_by_extensions_unrecognized_files(tmp_path):
    """Files with unrecognized extensions return 'unknown'."""
    (tmp_path / "data.csv").write_text("a,b,c")
    (tmp_path / "readme.txt").write_text("hello")
    assert _detect_by_extensions(str(tmp_path)) == "unknown"


# ── get_profile ───────────────────────────────────────────────────────────


def test_get_profile_known_language():
    """get_profile returns the correct profile for known languages."""
    p = get_profile("python")
    assert p.language == "python"
    assert ".py" in p.extensions


def test_get_profile_unknown_language():
    """get_profile returns a generic profile for unknown language strings."""
    p = get_profile("unknown")
    assert p.language == "unknown"
    assert "no built-in profile" in p.prompt_hints.lower() or "no default" in p.test_verify_command.lower()


def test_get_profile_generic_language():
    """get_profile returns generic profile for languages not in PROFILES."""
    p = get_profile("brainfuck")
    assert p.language == "brainfuck"


# ── Profile sanity checks ────────────────────────────────────────────────


def test_all_profiles_have_required_fields():
    """Every profile has non-empty language and at least one extension."""
    for lang, profile in PROFILES.items():
        assert profile.language == lang
        assert len(profile.extensions) > 0, f"{lang} has no extensions"
        assert len(profile.manifest_files) > 0 or lang == "html", f"{lang} has no manifest_files"


def test_python_profile_has_source_roots():
    """Python profile should have src/lib source roots for non-flat layouts."""
    p = PROFILES["python"]
    assert "src" in p.source_roots
    assert p.path_env_var == "PYTHONPATH"
