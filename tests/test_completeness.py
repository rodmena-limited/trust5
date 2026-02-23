"""Tests for ProjectCompletenessValidator and required_project_files."""

from __future__ import annotations

import os
import tempfile

from trust5.core.config import QualityConfig
from trust5.core.lang_profiles import PROFILES, LanguageProfile
from trust5.core.quality_models import PRINCIPLE_COMPLETENESS, PRINCIPLE_WEIGHTS
from trust5.core.quality_validators import ProjectCompletenessValidator


def _make_profile(required_files: tuple[str, ...] = ()) -> LanguageProfile:
    """Create a minimal LanguageProfile for testing."""
    return LanguageProfile(
        language="python",
        extensions=(".py",),
        test_command=("pytest",),
        test_verify_command="pytest",
        lint_commands=(),
        syntax_check_command=None,
        package_install_prefix="pip install",
        lsp_language_id="python",
        skip_dirs=(),
        manifest_files=("pyproject.toml",),
        prompt_hints="test",
        required_project_files=required_files,
    )


# ── LanguageProfile field tests ──────────────────────────────────────


def test_language_profile_has_required_project_files_field():
    """LanguageProfile dataclass has required_project_files field with default ()."""
    profile = _make_profile()
    assert hasattr(profile, "required_project_files")
    assert profile.required_project_files == ()


def test_python_profile_requires_pyproject_toml():
    """Python profile has pyproject.toml as required project file."""
    python = PROFILES["python"]
    assert python.required_project_files == ("pyproject.toml",)


def test_typescript_profile_requires_package_json_and_tsconfig():
    """TypeScript profile requires both package.json and tsconfig.json."""
    ts = PROFILES["typescript"]
    assert ts.required_project_files == ("package.json", "tsconfig.json")


def test_go_profile_requires_go_mod():
    go = PROFILES["go"]
    assert go.required_project_files == ("go.mod",)


def test_csharp_profile_has_empty_required_files():
    """C# uses glob manifests, so required_project_files is empty."""
    cs = PROFILES["csharp"]
    assert cs.required_project_files == ()


def test_html_profile_has_empty_required_files():
    html = PROFILES["html"]
    assert html.required_project_files == ()


def test_lua_profile_has_empty_required_files():
    lua = PROFILES["lua"]
    assert lua.required_project_files == ()


# ── PRINCIPLE_COMPLETENESS constant tests ────────────────────────────


def test_principle_completeness_constant():
    assert PRINCIPLE_COMPLETENESS == "completeness"


def test_completeness_weight_is_zero():
    """Completeness is a pass/fail gate, not a weighted pillar."""
    assert PRINCIPLE_WEIGHTS[PRINCIPLE_COMPLETENESS] == 0.0


# ── ProjectCompletenessValidator tests ───────────────────────────────


def test_validator_passes_when_required_files_exist():
    """Validator passes when all required files are present."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create the required file
        open(os.path.join(tmpdir, "pyproject.toml"), "w").close()
        profile = _make_profile(required_files=("pyproject.toml",))
        config = QualityConfig()
        validator = ProjectCompletenessValidator(tmpdir, profile, config)
        result = validator.validate()
        assert result.passed is True
        assert result.score == 1.0
        assert not any(i.rule == "required-file-missing" for i in result.issues)


def test_validator_fails_when_required_files_missing():
    """Validator fails when required files are absent."""
    with tempfile.TemporaryDirectory() as tmpdir:
        profile = _make_profile(required_files=("pyproject.toml",))
        config = QualityConfig()
        validator = ProjectCompletenessValidator(tmpdir, profile, config)
        result = validator.validate()
        assert result.passed is False
        assert any(i.rule == "required-file-missing" for i in result.issues)
        missing = [i for i in result.issues if i.rule == "required-file-missing"]
        assert "pyproject.toml" in missing[0].message


def test_validator_fails_partial_required_files():
    """Validator fails when only some required files exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "package.json"), "w").close()
        # tsconfig.json is missing
        profile = _make_profile(required_files=("package.json", "tsconfig.json"))
        config = QualityConfig()
        validator = ProjectCompletenessValidator(tmpdir, profile, config)
        result = validator.validate()
        assert result.passed is False
        missing = [i for i in result.issues if i.rule == "required-file-missing"]
        assert len(missing) == 1
        assert "tsconfig.json" in missing[0].message


def test_validator_detects_garbled_files():
    """Validator detects garbled files (e.g. =0 from shell redirect bugs)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a garbled file
        open(os.path.join(tmpdir, "=0"), "w").close()
        profile = _make_profile()
        config = QualityConfig()
        validator = ProjectCompletenessValidator(tmpdir, profile, config)
        result = validator.validate()
        assert result.passed is False
        garbled = [i for i in result.issues if i.rule == "garbled-file"]
        assert len(garbled) == 1
        assert "=0" in garbled[0].message


def test_validator_passes_with_no_required_files():
    """When no required files are specified, validator passes (no garbled files)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        profile = _make_profile(required_files=())
        config = QualityConfig()
        validator = ProjectCompletenessValidator(tmpdir, profile, config)
        result = validator.validate()
        assert result.passed is True
        assert result.score == 1.0


def test_validator_name():
    with tempfile.TemporaryDirectory() as tmpdir:
        profile = _make_profile()
        config = QualityConfig()
        validator = ProjectCompletenessValidator(tmpdir, profile, config)
        assert validator.name() == "completeness"
