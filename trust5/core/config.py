"""Trust5 configuration system with 3-tier precedence.

Precedence (highest wins):
  1. Environment variables (TRUST5_<SECTION>_<KEY>, e.g. TRUST5_AGENT_MAX_TURNS=30)
  2. Project config  (.trust5/config/sections/*.yaml)
  3. Global config   (~/.trust5/config.yaml)
  4. Pydantic defaults (hardcoded in this module)
"""

import logging
import os
import threading
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

_log = logging.getLogger(__name__)

# ── Global config location ───────────────────────────────────────────────────

GLOBAL_CONFIG_DIR = os.path.join(Path.home(), ".trust5")
GLOBAL_CONFIG_PATH = os.path.join(GLOBAL_CONFIG_DIR, "config.yaml")


# ── Sub-models (unchanged from previous) ─────────────────────────────────────


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


# ── Quality config ───────────────────────────────────────────────────────────


class QualityConfig(BaseModel):
    development_mode: str = "hybrid"
    coverage_threshold: float = 80.0
    pass_score_threshold: float = 0.70

    @field_validator("development_mode")
    @classmethod
    def _validate_development_mode(cls, v: str) -> str:
        allowed = ("tdd", "ddd", "hybrid")
        if v not in allowed:
            raise ValueError(f"development_mode must be one of {allowed}, got {v!r}")
        return v

    @field_validator("coverage_threshold")
    @classmethod
    def _validate_coverage_threshold(cls, v: float) -> float:
        if not 0 <= v <= 100:
            raise ValueError(f"coverage_threshold must be 0-100, got {v}")
        return v

    @field_validator("pass_score_threshold")
    @classmethod
    def _validate_pass_score_threshold(cls, v: float) -> float:
        if not 0 <= v <= 1:
            raise ValueError(f"pass_score_threshold must be 0-1, got {v}")
        return v

    @field_validator("max_quality_repairs")
    @classmethod
    def _validate_max_quality_repairs(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"max_quality_repairs must be >= 0, got {v}")
        return v

    @field_validator("max_jumps")
    @classmethod
    def _validate_max_jumps(cls, v: int) -> int:
        if v < 2:
            raise ValueError(f"max_jumps must be >= 2, got {v}")
        return v

    @field_validator("per_module_max_jumps")
    @classmethod
    def _validate_per_module_max_jumps(cls, v: int) -> int:
        if v < 2:
            raise ValueError(f"per_module_max_jumps must be >= 2, got {v}")
        return v

    @field_validator("max_repair_attempts")
    @classmethod
    def _validate_max_repair_attempts(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"max_repair_attempts must be >= 1, got {v}")
        return v

    @field_validator("max_reimplementations")
    @classmethod
    def _validate_max_reimplementations(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"max_reimplementations must be >= 0, got {v}")
        return v

    max_errors: int = 0
    max_type_errors: int = 0
    max_lint_errors: int = 0
    max_warnings: int = 10
    max_security_warnings: int = 0
    max_quality_repairs: int = 3
    # Pipeline repair loop limits (configurable via quality.yaml)
    max_jumps: int = 50
    per_module_max_jumps: int = 30
    max_repair_attempts: int = 5
    max_reimplementations: int = 3
    max_file_lines: int = 500
    enforce_quality: bool = True
    spec_compliance_threshold: float = 0.7
    spec_compliance_enabled: bool = True
    # LLM-based code review (semantic analysis between repair and quality gate)
    code_review_enabled: bool = True
    code_review_jump_to_repair: bool = False
    review_model_tier: str = "good"
    review_max_turns: int = 15
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


# ── Project-level section configs ────────────────────────────────────────────


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

    @field_validator("conversation_language")
    @classmethod
    def _validate_conversation_language(cls, v: str) -> str:
        if not 2 <= len(v) <= 5:
            raise ValueError(f"conversation_language must be 2-5 chars, got {v!r}")
        return v


class WorkflowConfig(BaseModel):
    team: dict[str, Any] = Field(default_factory=lambda: {"enabled": False})


class MoaiConfig(BaseModel):
    quality: QualityConfig = Field(default_factory=QualityConfig)
    git: GitStrategyConfig = Field(default_factory=GitStrategyConfig)
    language: LanguageConfig = Field(default_factory=LanguageConfig)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)


# ── Global config: runtime tunables ──────────────────────────────────────────


class AgentConfig(BaseModel):
    """Agent execution limits."""

    max_turns: int = 20
    max_history_messages: int = 60
    tool_result_limit: int = 8000
    default_timeout: int = 7200  # 2 hr wall-clock per agent run
    per_turn_timeout: int = 1800  # 30 min per LLM call
    idle_warn_turns: int = 5
    idle_max_turns: int = 10


class PipelineConfig(BaseModel):
    """Repair/validate loop tunables."""

    consecutive_failure_limit: int = 3
    max_repair_attempts: int = 5
    max_reimplementations: int = 3
    test_output_limit: int = 4000
    repair_agent_timeout: int = 1800
    quick_test_timeout: int = 60
    pytest_per_test_timeout: int = 30
    max_quality_attempts: int = 3
    setup_timeout: int = 120
    subprocess_timeout: int = 120


class WorkflowTimeoutConfig(BaseModel):
    """Workflow-level timeouts in seconds."""

    plan: float = 3600.0  # 1 hr
    develop: float = 864000.0  # 10 days
    run: float = 86400.0  # 1 day
    loop: float = 86400.0  # 1 day


class SubprocessConfig(BaseModel):
    """Subprocess execution timeouts in seconds."""

    bash_timeout: int = 600  # 10 min
    grep_timeout: int = 60
    syntax_check_timeout: int = 300  # 5 min
    test_run_timeout: int = 600  # 10 min


class StreamConfig(BaseModel):
    """LLM streaming timeouts in seconds."""

    read_timeout_thinking: int = 600
    read_timeout_standard: int = 120
    total_timeout: int = 3600
    retry_delay_server: int = 30


class MCPConfig(BaseModel):
    """MCP server settings."""

    start_timeout: float = 30.0
    process_stop_timeout: int = 5


class EventBusConfig(BaseModel):
    """Event bus settings."""

    socket_timeout: float = 5.0
    queue_batch_size: int = 64


class TUIConfig(BaseModel):
    """TUI display settings."""

    max_log_lines: int = 5000
    spinner_interval: float = 0.08
    elapsed_tick: float = 1.0


class WatchdogConfig(BaseModel):
    """Watchdog task settings."""

    check_interval: int = 12
    max_runtime: int = 7200  # 2 hours
    ok_emit_interval: int = 25
    max_llm_audits: int = 3


class LLMConfig(BaseModel):
    """LLM provider retry and timeout settings."""

    timeout_fast: int = 120
    timeout_standard: int = 300
    timeout_extended: int = 600
    connect_timeout: int = 10
    token_refresh_margin: int = 300
    retry_budget_connect: int = 3600
    retry_budget_server: int = 1800
    retry_budget_rate: int = 3600
    max_backoff_delay: float = 300.0


class GlobalConfig(BaseModel):
    """Top-level global configuration for Trust5.

    Written to ``~/.trust5/config.yaml`` on first run.
    Values here are defaults for ALL projects.  Per-project overrides
    go in ``.trust5/config/sections/*.yaml``.
    """

    agent: AgentConfig = Field(default_factory=AgentConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    timeouts: WorkflowTimeoutConfig = Field(default_factory=WorkflowTimeoutConfig)
    subprocess: SubprocessConfig = Field(default_factory=SubprocessConfig)
    stream: StreamConfig = Field(default_factory=StreamConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    event_bus: EventBusConfig = Field(default_factory=EventBusConfig)
    tui: TUIConfig = Field(default_factory=TUIConfig)
    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)


# ── Singleton: the resolved global config ────────────────────────────────────

_global_config: GlobalConfig | None = None
_config_lock: threading.Lock = threading.Lock()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into *base* (override wins)."""
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Apply TRUST5_<SECTION>_<KEY>=<value> environment variables.

    For example:
        TRUST5_AGENT_MAX_TURNS=30  → data["agent"]["max_turns"] = 30
        TRUST5_TIMEOUTS_DEVELOP=100000 → data["timeouts"]["develop"] = 100000.0
    """
    prefix = "TRUST5_"
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        parts = env_key[len(prefix) :].lower().split("_", 1)
        if len(parts) != 2:
            continue
        section, field = parts
        if section not in data:
            data[section] = {}
        if not isinstance(data[section], dict):
            continue
        # Coerce to the right type by trying int, then float, then string
        coerced: Any
        try:
            coerced = int(env_val)
        except ValueError:
            try:
                coerced = float(env_val)
            except ValueError:
                coerced = env_val
        data[section][field] = coerced
    return data


def load_global_config(force_reload: bool = False) -> GlobalConfig:
    """Load and cache the global config with 3-tier precedence.

    1. Pydantic defaults
    2. ``~/.trust5/config.yaml`` (global, written on first run)
    3. Environment variables ``TRUST5_<SECTION>_<KEY>``

    Project-level config is merged separately in ``ConfigManager.load_config()``.
    """
    global _global_config
    with _config_lock:
        if _global_config is not None and not force_reload:
            return _global_config

        data: dict[str, Any] = {}
        # Layer 1: Read global config file
        if os.path.exists(GLOBAL_CONFIG_PATH):
            try:
                with open(GLOBAL_CONFIG_PATH, encoding="utf-8") as f:
                    file_data = yaml.safe_load(f)
                    if isinstance(file_data, dict):
                        data = file_data
            except Exception as exc:
                _log.warning("Failed to read global config %s: %s", GLOBAL_CONFIG_PATH, exc)
        data = _apply_env_overrides(data)
        try:
            _global_config = GlobalConfig(**data)
        except (ValueError, TypeError) as exc:
            _log.warning("Invalid global config, using defaults: %s", exc)
            _global_config = GlobalConfig()
        return _global_config


def ensure_global_config() -> None:
    """Write the default global config to ``~/.trust5/config.yaml`` if it doesn't exist.

    Called on first run (``trust5 init``, ``trust5 develop``, etc.).
    """
    if os.path.exists(GLOBAL_CONFIG_PATH):
        return

    os.makedirs(GLOBAL_CONFIG_DIR, exist_ok=True)

    defaults = GlobalConfig()
    # Use model_dump to get a clean dict, then write as YAML
    data = defaults.model_dump()

    try:
        with open(GLOBAL_CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write("# Trust5 Global Configuration\n")
            f.write("# Edit values below to customize Trust5 behavior across ALL projects.\n")
            f.write("# Per-project overrides go in <project>/.trust5/config/sections/*.yaml\n")
            f.write("# Environment variables: TRUST5_<SECTION>_<KEY>=<value>\n\n")
            yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        _log.info("Wrote default global config to %s", GLOBAL_CONFIG_PATH)
    except OSError as exc:
        _log.warning("Failed to write global config to %s: %s", GLOBAL_CONFIG_PATH, exc)


# ── ConfigManager: project-level config with global fallback ─────────────────


class ConfigManager:
    """Load project-level configuration from ``.trust5/config/sections/*.yaml``.

    Project config is merged on top of the global config for shared fields
    (quality section).  Use ``get_global()`` to access runtime tunables.
    """

    def __init__(self, project_root: str = "."):
        self.project_root = project_root
        self.config_dir = os.path.join(project_root, ".trust5", "config")
        self.sections_dir = os.path.join(self.config_dir, "sections")
        self.config = MoaiConfig()

    def load_config(self) -> MoaiConfig:
        """Load project config from YAML sections, falling back to defaults."""
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
            try:
                self.config.quality = QualityConfig(**quality_data)
            except (ValueError, TypeError) as e:
                _log.warning("Invalid quality config, using defaults: %s", e)
                self.config.quality = QualityConfig()
        if git_data:
            try:
                self.config.git = GitStrategyConfig(**git_data)
            except (ValueError, TypeError) as e:
                _log.warning("Invalid git config, using defaults: %s", e)
        if lang_data:
            try:
                self.config.language = LanguageConfig(**lang_data)
            except (ValueError, TypeError) as e:
                _log.warning("Invalid language config, using defaults: %s", e)
        if workflow_data:
            try:
                self.config.workflow = WorkflowConfig(**workflow_data)
            except (ValueError, TypeError) as e:
                _log.warning("Invalid workflow config, using defaults: %s", e)

        return self.config

    @staticmethod
    def get_global() -> GlobalConfig:
        """Return the resolved global config (cached singleton)."""
        return load_global_config()

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
            _log.warning("Failed to load config %s: %s", path, e)
            return {}

    def get_config(self) -> MoaiConfig:
        return self.config
