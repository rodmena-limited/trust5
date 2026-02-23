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
    sections = tmp_path / ".trust5" / "config" / "sections"
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


# ---------------------------------------------------------------------------
# Config validation tests
# ---------------------------------------------------------------------------


def test_invalid_development_mode():
    """Invalid development_mode raises ValueError."""
    with pytest.raises(ValueError, match="development_mode must be one of"):
        QualityConfig(development_mode="waterfall")


def test_coverage_threshold_out_of_range():
    """coverage_threshold must be 0-100."""
    with pytest.raises(ValueError, match="coverage_threshold must be 0-100"):
        QualityConfig(coverage_threshold=150.0)


def test_pass_score_out_of_range():
    """pass_score_threshold must be 0-1."""
    with pytest.raises(ValueError, match="pass_score_threshold must be 0-1"):
        QualityConfig(pass_score_threshold=1.5)


def test_invalid_config_falls_back_to_defaults(tmp_path, config_dir):
    """Invalid quality YAML falls back to default config without crashing."""
    quality_yaml = {
        "quality": {
            "development_mode": "invalid_mode",
            "coverage_threshold": 80.0,
        }
    }
    (config_dir / "quality.yaml").write_text(yaml.dump(quality_yaml), encoding="utf-8")

    mgr = ConfigManager(project_root=str(tmp_path))
    cfg = mgr.load_config()
    # Should fall back to default rather than crash
    assert cfg.quality.development_mode == "hybrid"
    assert cfg.quality.coverage_threshold == 80.0


# ---------------------------------------------------------------------------
# Pipeline limit fields
# ---------------------------------------------------------------------------


def test_default_pipeline_limit_fields():
    """Default QualityConfig has correct pipeline limit defaults."""
    cfg = QualityConfig()
    assert cfg.max_jumps == 50
    assert cfg.per_module_max_jumps == 30
    assert cfg.max_repair_attempts == 5
    assert cfg.max_reimplementations == 3


def test_custom_pipeline_limit_values():
    """Custom pipeline limit values are accepted."""
    cfg = QualityConfig(
        max_jumps=100,
        per_module_max_jumps=60,
        max_repair_attempts=10,
        max_reimplementations=5,
    )
    assert cfg.max_jumps == 100
    assert cfg.per_module_max_jumps == 60
    assert cfg.max_repair_attempts == 10
    assert cfg.max_reimplementations == 5


def test_max_jumps_too_low():
    """max_jumps must be >= 2."""
    with pytest.raises(ValueError, match="max_jumps must be >= 2"):
        QualityConfig(max_jumps=1)


def test_per_module_max_jumps_too_low():
    """per_module_max_jumps must be >= 2."""
    with pytest.raises(ValueError, match="per_module_max_jumps must be >= 2"):
        QualityConfig(per_module_max_jumps=0)


def test_max_repair_attempts_too_low():
    """max_repair_attempts must be >= 1."""
    with pytest.raises(ValueError, match="max_repair_attempts must be >= 1"):
        QualityConfig(max_repair_attempts=0)


def test_max_reimplementations_negative():
    """max_reimplementations must be >= 0."""
    with pytest.raises(ValueError, match="max_reimplementations must be >= 0"):
        QualityConfig(max_reimplementations=-1)


def test_pipeline_limits_loaded_from_yaml(tmp_path, config_dir):
    """Pipeline limits are loaded from quality.yaml."""
    quality_yaml = {
        "quality": {
            "max_jumps": 80,
            "per_module_max_jumps": 40,
            "max_repair_attempts": 8,
            "max_reimplementations": 2,
        }
    }
    (config_dir / "quality.yaml").write_text(yaml.dump(quality_yaml), encoding="utf-8")

    mgr = ConfigManager(project_root=str(tmp_path))
    cfg = mgr.load_config()
    assert cfg.quality.max_jumps == 80
    assert cfg.quality.per_module_max_jumps == 40
    assert cfg.quality.max_repair_attempts == 8
    assert cfg.quality.max_reimplementations == 2


# ---------------------------------------------------------------------------
# GlobalConfig and global config loading
# ---------------------------------------------------------------------------


def test_global_config_defaults():
    from trust5.core.config import GlobalConfig

    cfg = GlobalConfig()
    assert cfg.agent.max_turns == 20
    assert cfg.agent.max_history_messages == 60
    assert cfg.agent.tool_result_limit == 8000
    assert cfg.agent.default_timeout == 7200
    assert cfg.pipeline.max_repair_attempts == 5
    assert cfg.pipeline.consecutive_failure_limit == 3
    assert cfg.pipeline.max_reimplementations == 3
    assert cfg.timeouts.plan == 3600.0
    assert cfg.timeouts.develop == 864000.0
    assert cfg.subprocess.bash_timeout == 600
    assert cfg.subprocess.grep_timeout == 60
    assert cfg.stream.read_timeout_thinking == 600
    assert cfg.mcp.start_timeout == 30.0
    assert cfg.event_bus.socket_timeout == 5.0
    assert cfg.tui.max_log_lines == 5000
    assert cfg.watchdog.check_interval == 12


def test_global_config_custom_values():
    from trust5.core.config import AgentConfig, GlobalConfig

    cfg = GlobalConfig(agent=AgentConfig(max_turns=50, idle_max_turns=20))
    assert cfg.agent.max_turns == 50
    assert cfg.agent.idle_max_turns == 20
    assert cfg.agent.max_history_messages == 60  # unchanged default


def test_deep_merge():
    from trust5.core.config import _deep_merge

    base = {"agent": {"max_turns": 20, "timeout": 100}, "stream": {"total": 3600}}
    override = {"agent": {"max_turns": 50}, "tui": {"lines": 1000}}
    result = _deep_merge(base, override)
    assert result["agent"]["max_turns"] == 50
    assert result["agent"]["timeout"] == 100
    assert result["stream"]["total"] == 3600
    assert result["tui"]["lines"] == 1000


def test_apply_env_overrides(monkeypatch):
    from trust5.core.config import _apply_env_overrides

    monkeypatch.setenv("TRUST5_AGENT_MAX_TURNS", "50")
    monkeypatch.setenv("TRUST5_TIMEOUTS_DEVELOP", "999999.5")
    data: dict = {"agent": {}, "timeouts": {}}
    result = _apply_env_overrides(data)
    assert result["agent"]["max_turns"] == 50
    assert result["timeouts"]["develop"] == 999999.5


def test_apply_env_overrides_string_value(monkeypatch):
    from trust5.core.config import _apply_env_overrides

    monkeypatch.setenv("TRUST5_AGENT_NAME", "custom-agent")
    data: dict = {"agent": {}}
    result = _apply_env_overrides(data)
    assert result["agent"]["name"] == "custom-agent"


def test_load_global_config_defaults(monkeypatch, tmp_path):
    import trust5.core.config as config_mod

    monkeypatch.setattr(config_mod, "_global_config", None)
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", str(tmp_path / "nonexistent.yaml"))
    cfg = config_mod.load_global_config(force_reload=True)
    assert cfg.agent.max_turns == 20
    assert cfg.timeouts.develop == 864000.0


def test_load_global_config_from_file(monkeypatch, tmp_path):
    import trust5.core.config as config_mod

    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"agent": {"max_turns": 99}, "tui": {"max_log_lines": 100}}), encoding="utf-8")
    monkeypatch.setattr(config_mod, "_global_config", None)
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", str(config_file))
    cfg = config_mod.load_global_config(force_reload=True)
    assert cfg.agent.max_turns == 99
    assert cfg.tui.max_log_lines == 100
    assert cfg.pipeline.max_repair_attempts == 5  # unchanged default


def test_load_global_config_caches(monkeypatch, tmp_path):
    import trust5.core.config as config_mod

    monkeypatch.setattr(config_mod, "_global_config", None)
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", str(tmp_path / "none.yaml"))
    cfg1 = config_mod.load_global_config(force_reload=True)
    cfg2 = config_mod.load_global_config()
    assert cfg1 is cfg2


def test_load_global_config_with_env_override(monkeypatch, tmp_path):
    import trust5.core.config as config_mod

    monkeypatch.setattr(config_mod, "_global_config", None)
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", str(tmp_path / "none.yaml"))
    monkeypatch.setenv("TRUST5_AGENT_MAX_TURNS", "77")
    cfg = config_mod.load_global_config(force_reload=True)
    assert cfg.agent.max_turns == 77


def test_load_global_config_invalid_file(monkeypatch, tmp_path):
    import trust5.core.config as config_mod

    bad_file = tmp_path / "bad.yaml"
    bad_file.write_text("{{{{invalid", encoding="utf-8")
    monkeypatch.setattr(config_mod, "_global_config", None)
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", str(bad_file))
    cfg = config_mod.load_global_config(force_reload=True)
    assert cfg.agent.max_turns == 20  # falls back to defaults


def test_ensure_global_config_creates_file(monkeypatch, tmp_path):
    import trust5.core.config as config_mod

    config_path = tmp_path / ".trust5" / "config.yaml"
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_DIR", str(tmp_path / ".trust5"))
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", str(config_path))
    config_mod.ensure_global_config()
    assert config_path.exists()
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "agent" in data
    assert data["agent"]["max_turns"] == 20


def test_ensure_global_config_does_not_overwrite(monkeypatch, tmp_path):
    import trust5.core.config as config_mod

    config_path = tmp_path / ".trust5" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("agent:\n  max_turns: 99\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_DIR", str(tmp_path / ".trust5"))
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", str(config_path))
    config_mod.ensure_global_config()
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["agent"]["max_turns"] == 99  # not overwritten


def test_config_manager_get_global(monkeypatch, tmp_path):
    import trust5.core.config as config_mod

    monkeypatch.setattr(config_mod, "_global_config", None)
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", str(tmp_path / "none.yaml"))
    cfg = ConfigManager.get_global()
    assert cfg.agent.max_turns == 20


# ---------------------------------------------------------------------------
# Constants module __getattr__
# ---------------------------------------------------------------------------


def test_constants_returns_int_values(monkeypatch):
    import trust5.core.config as config_mod

    monkeypatch.setattr(config_mod, "_global_config", None)
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", "/dev/null/nonexistent")
    from trust5.core import constants

    assert isinstance(constants.AGENT_MAX_TURNS, int)
    assert constants.AGENT_MAX_TURNS == 20
    assert isinstance(constants.TIMEOUT_DEVELOP, float)
    assert constants.TIMEOUT_DEVELOP == 864000.0
    assert isinstance(constants.BASH_TIMEOUT, int)
    assert constants.BASH_TIMEOUT == 600


def test_constants_reflects_config(monkeypatch, tmp_path):
    import trust5.core.config as config_mod

    config_file = tmp_path / "cfg.yaml"
    config_file.write_text(yaml.dump({"agent": {"max_turns": 42}}), encoding="utf-8")
    monkeypatch.setattr(config_mod, "_global_config", None)
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", str(config_file))
    config_mod.load_global_config(force_reload=True)
    from trust5.core import constants

    assert constants.AGENT_MAX_TURNS == 42


def test_constants_unknown_attr_raises():
    from trust5.core import constants

    with pytest.raises(AttributeError, match="has no attribute"):
        _ = constants.NONEXISTENT_CONSTANT
