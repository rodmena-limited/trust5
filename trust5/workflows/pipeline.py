import logging
import os

from stabilize import StageExecution, TaskExecution, Workflow

from ..core.config import ConfigManager
from ..core.lang import detect_language, get_profile

logger = logging.getLogger(__name__)

MAX_REPAIR_JUMPS = 50


def _load_development_mode(project_root: str) -> str:
    try:
        mgr = ConfigManager(project_root)
        cfg = mgr.load_config()
        return cfg.quality.development_mode
    except Exception:
        return "hybrid"


def _create_plan_stage(
    user_request: str,
    profile_dict: dict[str, object],
    dev_mode: str,
) -> StageExecution:
    """Build the plan stage (shared by serial and parallel pipelines)."""
    return StageExecution(
        ref_id="plan",
        type="agent",
        name="Plan (Create SPEC)",
        context={
            "agent_name": "trust5-planner",
            "prompt_file": "trust5-planner.md",
            "user_input": user_request,
            "model_tier": "good",
            "non_interactive": True,
            "language_profile": profile_dict,
            "development_mode": dev_mode,
        },
        requisite_stage_ref_ids=set(),
        tasks=[
            TaskExecution.create(
                name="Create SPEC",
                implementing_class="agent",
                stage_start=True,
                stage_end=True,
            ),
        ],
    )


def create_plan_only_workflow(user_request: str) -> Workflow:
    """Create a workflow that only runs the plan stage."""
    project_root = os.getcwd()
    language = detect_language(project_root)
    profile = get_profile(language)
    profile_dict = profile.to_dict()
    dev_mode = _load_development_mode(project_root)

    plan = _create_plan_stage(user_request, profile_dict, dev_mode)
    return Workflow.create(
        application="trust5",
        name="Plan Only",
        stages=[plan],
    )


def strip_plan_stage(
    stages: list[StageExecution],
    plan_output: str,
) -> list[StageExecution]:
    """Remove the plan stage and inject plan_output into the next stage's context.

    Used when plan was already executed in phase 1 and we're now running
    the implementation stages in phase 2 (serial fallback for N<=1 modules).
    """
    result: list[StageExecution] = []
    for stage in stages:
        if stage.ref_id == "plan":
            continue
        stage.requisite_stage_ref_ids = stage.requisite_stage_ref_ids - {"plan"}
        if plan_output:
            stage.context["plan_output"] = plan_output
            stage.context["ancestor_outputs"] = {"plan": plan_output}
        result.append(stage)
    return result


def _load_mutation_enabled(project_root: str) -> bool:
    """Check if mutation testing is enabled in config."""
    try:
        mgr = ConfigManager(project_root)
        cfg = mgr.load_config()
        return cfg.quality.tdd.mutation_testing_enabled or cfg.quality.test_quality.mutation_testing_enabled
    except Exception:
        return False


def _load_code_review_enabled(project_root: str) -> bool:
    """Check if LLM-based code review is enabled in config."""
    try:
        mgr = ConfigManager(project_root)
        cfg = mgr.load_config()
        return cfg.quality.code_review_enabled
    except Exception:
        return True  # enabled by default




def _load_pipeline_limits(project_root: str) -> dict[str, int]:
    """Load pipeline repair limits from config, with hardcoded fallbacks."""
    try:
        mgr = ConfigManager(project_root)
        cfg = mgr.load_config()
        return {
            "max_jumps": cfg.quality.max_jumps,
            "max_repair_attempts": cfg.quality.max_repair_attempts,
            "max_reimplementations": cfg.quality.max_reimplementations,
            "per_module_max_jumps": cfg.quality.per_module_max_jumps,
        }
    except Exception:
        return {
            "max_jumps": 50,
            "max_repair_attempts": 5,
            "max_reimplementations": 3,
            "per_module_max_jumps": 30,
        }

def create_develop_workflow(user_request: str) -> Workflow:
    project_root = os.getcwd()

    language = detect_language(project_root)
    profile = get_profile(language)
    profile_dict = profile.to_dict()
    dev_mode = _load_development_mode(project_root)
    limits = _load_pipeline_limits(project_root)

    use_tdd = dev_mode in ("tdd", "hybrid")
    use_mutation = _load_mutation_enabled(project_root)

    logger.info("Pipeline development_mode=%s (use_tdd=%s, mutation=%s)", dev_mode, use_tdd, use_mutation)

    plan = _create_plan_stage(user_request, profile_dict, dev_mode)

    setup = StageExecution(
        ref_id="setup",
        type="setup",
        name="Setup Environment",
        context={
            "project_root": project_root,
            "setup_commands": [],
        },
        requisite_stage_ref_ids={"plan"},
        tasks=[
            TaskExecution.create(
                name="Setup Environment",
                implementing_class="setup",
                stage_start=True,
                stage_end=True,
            ),
        ],
    )

    write_tests = StageExecution(
        ref_id="write_tests",
        type="agent",
        name="Write Tests (TDD RED Phase)",
        context={
            "agent_name": "test-writer",
            "prompt_file": "test-writer.md",
            "user_input": user_request,
            "model_tier": "good",
            "max_turns": 15,
            "non_interactive": True,
            "language_profile": profile_dict,
            "development_mode": dev_mode,
            "pipeline_phase": "run",
        },
        requisite_stage_ref_ids={"setup"},
        tasks=[
            TaskExecution.create(
                name="Write Specification Tests",
                implementing_class="agent",
                stage_start=True,
                stage_end=True,
            ),
        ],
    )

    implement_deps: set[str] = {"write_tests"} if use_tdd else {"setup"}

    implement = StageExecution(
        ref_id="implement",
        type="agent",
        name="Implement (Generate Code)",
        context={
            "agent_name": "implementer",
            "prompt_file": "implementer.md",
            "user_input": user_request,
            "model_tier": "best",
            "max_turns": 25,
            "non_interactive": True,
            "language_profile": profile_dict,
            "development_mode": dev_mode,
            "test_first_completed": use_tdd,
        },
        requisite_stage_ref_ids=implement_deps,
        tasks=[
            TaskExecution.create(
                name="Generate Code",
                implementing_class="agent",
                stage_start=True,
                stage_end=True,
            ),
        ],
    )

    validate = StageExecution(
        ref_id="validate",
        type="validate",
        name="Validate (Run Tests)",
        context={
            "project_root": project_root,
            "max_repair_attempts": limits["max_repair_attempts"],
            "max_reimplementations": limits["max_reimplementations"],
            "_max_jumps": limits["max_jumps"],
            "language_profile": profile_dict,
            "pipeline_phase": "run",
            "development_mode": dev_mode,
            "test_first_completed": use_tdd,
        },
        requisite_stage_ref_ids={"implement"},
        tasks=[
            TaskExecution.create(
                name="Run Tests",
                implementing_class="validate",
                stage_start=True,
                stage_end=True,
            ),
        ],
    )

    repair = StageExecution(
        ref_id="repair",
        type="repair",
        name="Repair (Fix Failures)",
        context={
            "project_root": project_root,
            "_max_jumps": limits["max_jumps"],
            "language_profile": profile_dict,
            "development_mode": dev_mode,
        },
        requisite_stage_ref_ids={"validate"},
        tasks=[
            TaskExecution.create(
                name="Fix Code",
                implementing_class="repair",
                stage_start=True,
                stage_end=True,
            ),
        ],
    )

    # Optional mutation testing stage (Oracle Problem mitigation)
    review_deps: set[str] = {"repair"}
    if use_mutation:
        mutation = StageExecution(
            ref_id="mutation",
            type="mutation",
            name="Mutation Testing (Oracle Gate)",
            context={
                "project_root": project_root,
                "language_profile": profile_dict,
                "max_mutation_samples": 10,
            },
            requisite_stage_ref_ids={"repair"},
            tasks=[
                TaskExecution.create(
                    name="Mutation Testing",
                    implementing_class="mutation",
                    stage_start=True,
                    stage_end=True,
                ),
            ],
        )
        review_deps = {"mutation"}

    # Optional LLM-based code review (semantic analysis)
    use_review = _load_code_review_enabled(project_root)
    quality_deps: set[str] = review_deps.copy()
    if use_review:
        review = StageExecution(
            ref_id="review",
            type="review",
            name="Review (Code Analysis)",
            context={
                "project_root": project_root,
                "language_profile": profile_dict,
            },
            requisite_stage_ref_ids=review_deps,
            tasks=[
                TaskExecution.create(
                    name="Code Review",
                    implementing_class="review",
                    stage_start=True,
                    stage_end=True,
                ),
            ],
        )
        quality_deps = {"review"}

    quality = StageExecution(
        ref_id="quality",
        type="quality",
        name="Quality (TRUST 5 Gate)",
        context={
            "project_root": project_root,
            "quality_attempt": 0,
            "max_quality_attempts": 3,
            "_max_jumps": limits["max_jumps"],
            "language_profile": profile_dict,
            "pipeline_phase": "run",
            "development_mode": dev_mode,
            "test_first_completed": use_tdd,
        },
        requisite_stage_ref_ids=quality_deps,
        tasks=[
            TaskExecution.create(
                name="TRUST 5 Quality Gate",
                implementing_class="quality",
                stage_start=True,
                stage_end=True,
            ),
        ],
    )

    watchdog = StageExecution(
        ref_id="watchdog",
        type="watchdog",
        name="Watchdog (Pipeline Monitor)",
        context={
            "project_root": project_root,
            "language_profile": profile_dict,
        },
        requisite_stage_ref_ids=set(),
        tasks=[
            TaskExecution.create(
                name="Pipeline Health Monitor",
                implementing_class="watchdog",
                stage_start=True,
                stage_end=True,
            )
        ],
    )

    stages = [plan, setup, watchdog]
    if use_tdd:
        stages.append(write_tests)
    stages.extend([implement, validate, repair])
    if use_mutation:
        stages.append(mutation)
    if use_review:
        stages.append(review)
    stages.append(quality)

    return Workflow.create(
        application="trust5",
        name="Develop Pipeline",
        stages=stages,
    )
