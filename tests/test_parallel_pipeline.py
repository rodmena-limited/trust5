from __future__ import annotations
from unittest.mock import MagicMock
from stabilize import StageExecution, TaskExecution, Workflow
from stabilize.models.status import WorkflowStatus
from trust5.workflows.parallel_pipeline import (
    ModuleSpec,
    create_parallel_develop_workflow,
    extract_plan_output,
    parse_modules,
)
from trust5.workflows.pipeline import strip_plan_stage

def _make_workflow(plan_output: str) -> Workflow:
    plan_stage = MagicMock(spec=StageExecution)
    plan_stage.ref_id = "plan"
    plan_stage.outputs = {"response": plan_output}
    plan_stage.status = WorkflowStatus.SUCCEEDED

    wf = MagicMock(spec=Workflow)
    wf.stages = [plan_stage]
    return wf

class TestParseModules:

    def test_no_modules_block_returns_default(self) -> None:
        wf = _make_workflow("Just a regular plan with no modules.")
        result = parse_modules(wf)
        assert len(result) == 1
        assert result[0].id == "main"

    def test_valid_modules_block(self) -> None:
        plan = (
            "Some plan text\n"
            "<!-- MODULES\n"
            '[{"id": "auth", "name": "Auth", "files": ["src/auth.py"], '
            '"test_files": ["tests/test_auth.py"], "deps": []},'
            '{"id": "api", "name": "API", "files": ["src/api.py"], '
            '"test_files": ["tests/test_api.py"], "deps": ["auth"]}]\n'
            "-->\n"
            "More text"
        )
        wf = _make_workflow(plan)
        result = parse_modules(wf)
        assert len(result) == 2
        assert result[0].id == "auth"
        assert result[0].files == ["src/auth.py"]
        assert result[0].deps == []
        assert result[1].id == "api"
        assert result[1].deps == ["auth"]

    def test_malformed_json_returns_default(self) -> None:
        plan = "<!-- MODULES\n{not valid json}\n-->"
        wf = _make_workflow(plan)
        result = parse_modules(wf)
        assert len(result) == 1
        assert result[0].id == "main"
