import os

from stabilize import StageExecution, TaskExecution, Workflow

from ..core.config import ConfigManager
from ..core.context_builder import build_implementation_prompt


def create_run_workflow(spec_id: str) -> Workflow:
    config_manager = ConfigManager(os.getcwd())
    config = config_manager.load_config()

    dev_mode = config.quality.development_mode

    if config.workflow.team.get("enabled", False):
        return _create_team_run_workflow(spec_id)

    project_root = os.getcwd()
    user_prompt = build_implementation_prompt(spec_id, project_root)

    stages = [
        StageExecution(
            ref_id="implementation_stage",
            type="agent",
            name="Implementation",
            context={
                "agent_name": "implementer",
                "prompt_file": "implementer.md",
                "user_input": user_prompt,
            },
            tasks=[
                TaskExecution.create(
                    name="Execute Implementer",
                    implementing_class="agent",
                    stage_start=True,
                    stage_end=True,
                ),
            ],
        ),
    ]

    return Workflow.create(
        application="trust5",
        name=f"Run Phase ({dev_mode})",
        stages=stages,
    )


def _create_team_run_workflow(spec_id: str) -> Workflow:
    stages = [
        StageExecution(
            ref_id="backend_stage",
            type="agent",
            name="Team Backend Dev",
            context={
                "agent_name": "team-backend-dev",
                "prompt_file": "team-backend-dev.md",
                "user_input": f"Implement Backend for {spec_id}",
            },
            tasks=[TaskExecution.create("Backend", "agent", stage_start=True, stage_end=True)],
        ),
        StageExecution(
            ref_id="frontend_stage",
            type="agent",
            name="Team Frontend Dev",
            context={
                "agent_name": "team-frontend-dev",
                "prompt_file": "team-frontend-dev.md",
                "user_input": f"Implement Frontend for {spec_id}",
            },
            tasks=[TaskExecution.create("Frontend", "agent", stage_start=True, stage_end=True)],
        ),
        StageExecution(
            ref_id="tester_stage",
            type="agent",
            name="Team Tester",
            context={
                "agent_name": "team-tester",
                "prompt_file": "team-tester.md",
                "user_input": f"Create tests for {spec_id}",
            },
            tasks=[TaskExecution.create("Tester", "agent", stage_start=True, stage_end=True)],
        ),
        StageExecution(
            ref_id="quality_stage",
            type="agent",
            name="Team Quality",
            context={
                "agent_name": "team-quality",
                "prompt_file": "team-quality.md",
                "user_input": f"Verify quality for {spec_id}",
            },
            tasks=[TaskExecution.create("Quality", "agent", stage_start=True, stage_end=True)],
        ),
    ]

    return Workflow.create(
        application="trust5",
        name="Run Phase (Team)",
        stages=stages,
    )
