from stabilize import StageExecution, TaskExecution, Workflow


def create_loop_workflow() -> Workflow:
    return Workflow.create(
        application="trust5",
        name="Ralph Loop",
        stages=[
            StageExecution(
                ref_id="loop_stage",
                type="loop",
                name="Ralph Autonomous Fix Loop",
                context={},
                tasks=[
                    TaskExecution.create(
                        name="Execute Ralph Loop",
                        implementing_class="loop",
                        stage_start=True,
                        stage_end=True,
                    ),
                ],
            ),
        ],
    )
