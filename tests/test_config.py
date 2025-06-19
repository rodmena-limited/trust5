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

def config_dir(tmp_path):
    sections = tmp_path / ".moai" / "config" / "sections"
    sections.mkdir(parents=True)
    return sections

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
