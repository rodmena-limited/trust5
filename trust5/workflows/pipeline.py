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
