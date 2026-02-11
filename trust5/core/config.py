import logging
import os
from typing import Any

import yaml
from pydantic import BaseModel, Field


class PlanGateConfig(BaseModel):
    require_baseline: bool = True


class RunGateConfig(BaseModel):
    max_errors: int = 0
    max_type_errors: int = 0
    max_lint_errors: int = 0
    allow_regression: bool = False


class SyncGateConfig(BaseModel):
    max_errors: int = 0
    max_warnings: int = 10
    require_clean_lsp: bool = True


class RegressionConfig(BaseModel):
    error_increase_threshold: int = 0
    warning_increase_threshold: int = 10
    type_error_increase_threshold: int = 0


class DDDConfig(BaseModel):
    require_existing_tests: bool = True
    characterization_tests: bool = True
    behavior_snapshots: bool = True
    max_transformation_size: str = "medium"
    preserve_before_improve: bool = True


class TDDConfig(BaseModel):
    min_coverage_per_commit: int = 80
    require_test_first: bool = True
    red_green_refactor: bool = True
    mutation_testing_enabled: bool = False


class HybridConfig(BaseModel):
    new_features: str = "tdd"
    legacy_refactoring: str = "ddd"
    min_coverage_new: int = 90
    min_coverage_legacy: int = 85
    preserve_refactoring: bool = True


class CoverageExemptions(BaseModel):
    enabled: bool = False
    require_justification: bool = True
    max_exempt_percentage: int = 20


class TestQuality(BaseModel):
    specification_based: bool = True
    meaningful_assertions: bool = True
    avoid_implementation_coupling: bool = True
    mutation_testing_enabled: bool = False


class SimplicityPrinciple(BaseModel):
    max_parallel_tasks: int = 5


class ReportGeneration(BaseModel):
    enabled: bool = True
    auto_create: bool = True


class QualityConfig(BaseModel):
    development_mode: str = "hybrid"
    coverage_threshold: float = 80.0
    pass_score_threshold: float = 0.70
    max_errors: int = 0
    max_type_errors: int = 0
    max_lint_errors: int = 0
    max_warnings: int = 10
    max_security_warnings: int = 0
    max_quality_repairs: int = 3
    max_file_lines: int = 500
    enforce_quality: bool = True
    spec_compliance_threshold: float = 0.7
    spec_compliance_enabled: bool = True
    # LLM-driven overrides (set from planner output, not YAML config)
    plan_lint_command: str | None = None
    plan_test_command: str | None = None
    plan_coverage_command: str | None = None
    plan_gate: PlanGateConfig = Field(default_factory=PlanGateConfig)
    run_gate: RunGateConfig = Field(default_factory=RunGateConfig)
    sync_gate: SyncGateConfig = Field(default_factory=SyncGateConfig)
    regression: RegressionConfig = Field(default_factory=RegressionConfig)
    ddd: DDDConfig = Field(default_factory=DDDConfig)
    tdd: TDDConfig = Field(default_factory=TDDConfig)
    hybrid: HybridConfig = Field(default_factory=HybridConfig)
    coverage_exemptions: CoverageExemptions = Field(default_factory=CoverageExemptions)
    test_quality: TestQuality = Field(default_factory=TestQuality)
    simplicity: SimplicityPrinciple = Field(default_factory=SimplicityPrinciple)
    report_generation: ReportGeneration = Field(default_factory=ReportGeneration)


class GitStrategyConfig(BaseModel):
    auto_branch: bool = True
    branch_prefix: str = "feature/"
    spec_git_workflow: str = "main_direct"
    team: dict[str, Any] = Field(default_factory=lambda: {"enabled": False})


class LanguageConfig(BaseModel):
    conversation_language: str = "en"
    code_comments: str = "en"
    language: str = "auto"
    test_framework: str = "auto"
    lsp_command: list[str] = Field(default_factory=list)


class WorkflowConfig(BaseModel):
    team: dict[str, Any] = Field(default_factory=lambda: {"enabled": False})


class MoaiConfig(BaseModel):
    quality: QualityConfig = Field(default_factory=QualityConfig)
    git: GitStrategyConfig = Field(default_factory=GitStrategyConfig)
    language: LanguageConfig = Field(default_factory=LanguageConfig)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)


class ConfigManager:
    def __init__(self, project_root: str = "."):
        self.project_root = project_root
        self.config_dir = os.path.join(project_root, ".moai", "config")
        self.sections_dir = os.path.join(self.config_dir, "sections")
        self.config = MoaiConfig()

    def load_config(self) -> MoaiConfig:
        quality_path = os.path.join(self.sections_dir, "quality.yaml")
        git_path = os.path.join(self.sections_dir, "git-strategy.yaml")
        lang_path = os.path.join(self.sections_dir, "language.yaml")
        workflow_path = os.path.join(self.sections_dir, "workflow.yaml")

        quality_data = self._unwrap(self._load_yaml(quality_path), "quality")
        git_data = self._unwrap(self._load_yaml(git_path), "git_strategy")
        lang_data = self._unwrap(self._load_yaml(lang_path), "language")
        workflow_data = self._unwrap(self._load_yaml(workflow_path), "workflow")

        if quality_data:
            quality_data = self._flatten_lsp_gates(quality_data)
            self.config.quality = QualityConfig(**quality_data)
        if git_data:
            self.config.git = GitStrategyConfig(**git_data)
        if lang_data:
            self.config.language = LanguageConfig(**lang_data)
        if workflow_data:
            self.config.workflow = WorkflowConfig(**workflow_data)

        return self.config

    @staticmethod
    def _flatten_lsp_gates(data: dict[str, Any]) -> dict[str, Any]:
        gates = data.pop("lsp_quality_gates", None)
        if gates and isinstance(gates, dict):
            run_gates = gates.get("run", {})
            if isinstance(run_gates, dict):
                for key, value in run_gates.items():
                    if key not in data:
                        data[key] = value
        return data

    @staticmethod
    def _unwrap(data: dict[str, Any], key: str) -> dict[str, Any]:
        if key in data and isinstance(data[key], dict):
            nested: dict[str, Any] = data[key]
            return nested
        return data

    def _load_yaml(self, path: str) -> dict[str, Any]:
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                result: dict[str, Any] = yaml.safe_load(f) or {}
                return result
        except Exception as e:
            logging.getLogger(__name__).warning("Failed to load config %s: %s", path, e)
            return {}

    def get_config(self) -> MoaiConfig:
        return self.config
