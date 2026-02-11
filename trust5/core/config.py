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
    max_transformation_size: str = 'medium'
    preserve_before_improve: bool = True

class TDDConfig(BaseModel):
    min_coverage_per_commit: int = 80
    require_test_first: bool = True
    red_green_refactor: bool = True
    mutation_testing_enabled: bool = False

class HybridConfig(BaseModel):
    new_features: str = 'tdd'
    legacy_refactoring: str = 'ddd'
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
    development_mode: str = 'hybrid'
    coverage_threshold: float = 80.0
    pass_score_threshold: float = 0.7
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
