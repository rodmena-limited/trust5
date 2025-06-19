"""Tests for trust5.core.config module."""

import pytest
import yaml

from trust5.core.config import (
    ConfigManager,
    GitStrategyConfig,
    LanguageConfig,
    MoaiConfig,
    QualityConfig,
    WorkflowConfig,
)


@pytest.fixture
def config_dir(tmp_path):
    sections = tmp_path / ".moai" / "config" / "sections"
    sections.mkdir(parents=True)
    return sections


# ---------------------------------------------------------------------------
# Pydantic model defaults
# ---------------------------------------------------------------------------


def test_default_quality_config():
    cfg = QualityConfig()
    assert cfg.coverage_threshold == 80.0
    assert cfg.pass_score_threshold == 0.70
    assert cfg.development_mode == "hybrid"
    assert cfg.max_errors == 0
    assert cfg.max_type_errors == 0
    assert cfg.max_lint_errors == 0
    assert cfg.max_warnings == 10
    assert cfg.max_security_warnings == 0
    assert cfg.max_quality_repairs == 3
    assert cfg.max_file_lines == 500
    assert cfg.enforce_quality is True
    assert cfg.plan_lint_command is None
    assert cfg.plan_test_command is None
    assert cfg.plan_coverage_command is None
    # Sub-model defaults
    assert cfg.plan_gate.require_baseline is True
    assert cfg.run_gate.max_errors == 0
    assert cfg.run_gate.allow_regression is False
    assert cfg.sync_gate.require_clean_lsp is True
    assert cfg.regression.error_increase_threshold == 0
    assert cfg.ddd.characterization_tests is True
    assert cfg.tdd.min_coverage_per_commit == 80
    assert cfg.hybrid.new_features == "tdd"
    assert cfg.coverage_exemptions.enabled is False
    assert cfg.test_quality.specification_based is True
    assert cfg.simplicity.max_parallel_tasks == 5
    assert cfg.report_generation.enabled is True


def test_default_moai_config():
    cfg = MoaiConfig()
    assert isinstance(cfg.quality, QualityConfig)
    assert isinstance(cfg.git, GitStrategyConfig)
    assert isinstance(cfg.language, LanguageConfig)
    assert isinstance(cfg.workflow, WorkflowConfig)
    # Spot-check sub-config defaults
    assert cfg.quality.coverage_threshold == 80.0
    assert cfg.git.auto_branch is True
    assert cfg.git.branch_prefix == "feature/"
    assert cfg.language.conversation_language == "en"
    assert cfg.language.language == "auto"
    assert cfg.language.test_framework == "auto"
    assert cfg.language.lsp_command == []
    assert cfg.workflow.team == {"enabled": False}


def test_quality_config_custom_values():
    cfg = QualityConfig(
        development_mode="tdd",
        coverage_threshold=90.0,
        pass_score_threshold=0.80,
        max_errors=5,
        max_warnings=20,
        enforce_quality=False,
    )
    assert cfg.development_mode == "tdd"
    assert cfg.coverage_threshold == 90.0
    assert cfg.pass_score_threshold == 0.80
    assert cfg.max_errors == 5
    assert cfg.max_warnings == 20
    assert cfg.enforce_quality is False
    # Unchanged defaults
    assert cfg.max_type_errors == 0
    assert cfg.max_file_lines == 500


# ---------------------------------------------------------------------------
# ConfigManager — load_config
# ---------------------------------------------------------------------------


def test_load_config_missing_dir(tmp_path):
    """ConfigManager with a nonexistent config directory returns all defaults."""
    mgr = ConfigManager(project_root=str(tmp_path / "nonexistent"))
    cfg = mgr.load_config()
    assert cfg.quality.coverage_threshold == 80.0
    assert cfg.quality.pass_score_threshold == 0.70
    assert cfg.git.auto_branch is True
    assert cfg.language.conversation_language == "en"
    assert cfg.workflow.team == {"enabled": False}


def test_load_config_from_yaml(tmp_path, config_dir):
    """Loading real YAML files populates the config correctly."""
    quality_yaml = {
        "quality": {
            "development_mode": "ddd",
            "coverage_threshold": 90.0,
            "pass_score_threshold": 0.85,
            "max_errors": 2,
        }
    }
    git_yaml = {
        "git_strategy": {
            "auto_branch": False,
            "branch_prefix": "bugfix/",
            "spec_git_workflow": "branch_per_spec",
        }
    }
    lang_yaml = {
        "language": {
            "conversation_language": "ko",
            "code_comments": "ko",
            "language": "go",
            "test_framework": "go test",
            "lsp_command": ["gopls", "serve"],
        }
    }
    workflow_yaml = {
        "workflow": {
            "team": {"enabled": True, "max_size": 5},
        }
    }

    (config_dir / "quality.yaml").write_text(yaml.dump(quality_yaml), encoding="utf-8")
    (config_dir / "git-strategy.yaml").write_text(yaml.dump(git_yaml), encoding="utf-8")
    (config_dir / "language.yaml").write_text(yaml.dump(lang_yaml), encoding="utf-8")
    (config_dir / "workflow.yaml").write_text(yaml.dump(workflow_yaml), encoding="utf-8")

    mgr = ConfigManager(project_root=str(tmp_path))
    cfg = mgr.load_config()

    assert cfg.quality.development_mode == "ddd"
    assert cfg.quality.coverage_threshold == 90.0
    assert cfg.quality.pass_score_threshold == 0.85
    assert cfg.quality.max_errors == 2

    assert cfg.git.auto_branch is False
    assert cfg.git.branch_prefix == "bugfix/"
    assert cfg.git.spec_git_workflow == "branch_per_spec"

    assert cfg.language.conversation_language == "ko"
    assert cfg.language.language == "go"
    assert cfg.language.test_framework == "go test"
    assert cfg.language.lsp_command == ["gopls", "serve"]

    assert cfg.workflow.team == {"enabled": True, "max_size": 5}


# ---------------------------------------------------------------------------
# ConfigManager — _unwrap
# ---------------------------------------------------------------------------


def test_unwrap_nested_key():
    """_unwrap extracts the inner dict when the key matches a nested dict."""
    data = {"quality": {"coverage_threshold": 95.0, "max_errors": 1}}
    result = ConfigManager._unwrap(data, "quality")
    assert result == {"coverage_threshold": 95.0, "max_errors": 1}


def test_unwrap_flat():
    """_unwrap returns original dict when the key is absent."""
    data = {"coverage_threshold": 95.0, "max_errors": 1}
    result = ConfigManager._unwrap(data, "quality")
    assert result == {"coverage_threshold": 95.0, "max_errors": 1}


def test_unwrap_non_dict_value():
    """_unwrap returns original dict when the key exists but value is not a dict."""
    data = {"quality": "high"}
    result = ConfigManager._unwrap(data, "quality")
    assert result == {"quality": "high"}


# ---------------------------------------------------------------------------
# ConfigManager — _flatten_lsp_gates
# ---------------------------------------------------------------------------


def test_flatten_lsp_gates():
    """lsp_quality_gates.run values are promoted to top-level keys."""
    data = {
        "development_mode": "hybrid",
        "lsp_quality_gates": {
            "run": {
                "max_errors": 3,
                "max_type_errors": 2,
                "max_lint_errors": 1,
            }
        },
    }
    result = ConfigManager._flatten_lsp_gates(data)
    assert result["max_errors"] == 3
    assert result["max_type_errors"] == 2
    assert result["max_lint_errors"] == 1
    assert result["development_mode"] == "hybrid"
    assert "lsp_quality_gates" not in result


def test_flatten_lsp_gates_no_override():
    """Existing top-level keys are NOT overridden by lsp_quality_gates values."""
    data = {
        "max_errors": 10,
        "lsp_quality_gates": {
            "run": {
                "max_errors": 0,
            }
        },
    }
    result = ConfigManager._flatten_lsp_gates(data)
    assert result["max_errors"] == 10


def test_flatten_lsp_gates_absent():
    """When lsp_quality_gates is absent, data is returned unchanged."""
    data = {"development_mode": "tdd", "coverage_threshold": 90.0}
    result = ConfigManager._flatten_lsp_gates(data)
    assert result == {"development_mode": "tdd", "coverage_threshold": 90.0}


# ---------------------------------------------------------------------------
# ConfigManager — _load_yaml edge cases
# ---------------------------------------------------------------------------


def test_load_yaml_missing_file(tmp_path):
    """Missing YAML file returns empty dict without error."""
    mgr = ConfigManager(project_root=str(tmp_path))
    result = mgr._load_yaml(str(tmp_path / "nonexistent.yaml"))
    assert result == {}


def test_load_yaml_invalid_file(tmp_path):
    """Corrupt YAML returns empty dict (no crash)."""
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("{{{{invalid yaml: [unterminated", encoding="utf-8")
    mgr = ConfigManager(project_root=str(tmp_path))
    result = mgr._load_yaml(str(bad_yaml))
    assert result == {}


def test_load_yaml_empty_file(tmp_path):
    """Empty YAML file returns empty dict."""
    empty_yaml = tmp_path / "empty.yaml"
    empty_yaml.write_text("", encoding="utf-8")
    mgr = ConfigManager(project_root=str(tmp_path))
    result = mgr._load_yaml(str(empty_yaml))
    assert result == {}


# ---------------------------------------------------------------------------
# ConfigManager — get_config
# ---------------------------------------------------------------------------


def test_get_config_returns_current_state(tmp_path):
    """get_config returns the config object reflecting the latest load."""
    mgr = ConfigManager(project_root=str(tmp_path))
    cfg_before = mgr.get_config()
    assert cfg_before.quality.coverage_threshold == 80.0

    # After loading (even with no files), still returns default config
    mgr.load_config()
    cfg_after = mgr.get_config()
    assert cfg_after.quality.coverage_threshold == 80.0
