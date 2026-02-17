import os

from stabilize import StageExecution, TaskExecution, Workflow

from ..core.config import ConfigManager


def create_plan_workflow(user_request: str) -> Workflow:
    config_manager = ConfigManager(os.getcwd())
    config = config_manager.load_config()

    if config.workflow.team.get("enabled", False):
        return _create_team_plan_workflow(user_request)

    return Workflow.create(
        application="trust5",
        name="Plan Phase",
        stages=[
            StageExecution(
                ref_id="plan_stage",
                type="agent",
                name="Manager Spec",
                context={
                    "agent_name": "manager-spec",
                    "prompt_file": "manager-spec.md",
                    "user_input": user_request,
                },
                tasks=[
                    TaskExecution.create(
                        name="Execute Manager Spec",
                        implementing_class="agent",
                        stage_start=True,
                        stage_end=True,
                    ),
                ],
            ),
        ],
    )


def _create_team_plan_workflow(user_request: str) -> Workflow:
    research_stage = StageExecution(
        ref_id="research_stage",
        type="agent",
        name="Team Researcher",
        context={
            "agent_name": "team-researcher",
            "prompt_file": "team-researcher.md",
            "user_input": f"Research: {user_request}",
        },
        tasks=[TaskExecution.create("Research", "agent", stage_start=True, stage_end=True)],
    )

    analyst_stage = StageExecution(
        ref_id="analyst_stage",
        type="agent",
        name="Team Analyst",
        context={
            "agent_name": "team-analyst",
            "prompt_file": "team-analyst.md",
            "user_input": f"Analyze: {user_request}",
        },
        tasks=[TaskExecution.create("Analyze", "agent", stage_start=True, stage_end=True)],
    )

    architect_stage = StageExecution(
        ref_id="architect_stage",
        type="agent",
        name="Team Architect",
        context={
            "agent_name": "team-architect",
            "prompt_file": "team-architect.md",
            "user_input": f"Architect: {user_request}",
        },
        tasks=[TaskExecution.create("Architect", "agent", stage_start=True, stage_end=True)],
    )

    spec_stage = StageExecution(
        ref_id="spec_stage",
        type="agent",
        name="Manager Spec (Consolidate)",
        context={
            "agent_name": "manager-spec",
            "prompt_file": "manager-spec.md",
            "user_input": f"Consolidate team findings and create SPEC for: {user_request}",
        },
        tasks=[TaskExecution.create("Consolidate", "agent", stage_start=True, stage_end=True)],
    )

    return Workflow.create(
        application="trust5",
        name="Plan Phase (Team)",
        stages=[research_stage, analyst_stage, architect_stage, spec_stage],
    )
