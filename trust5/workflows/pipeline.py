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
