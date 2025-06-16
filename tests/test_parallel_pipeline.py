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

    def test_empty_array_returns_default(self) -> None:
        plan = "<!-- MODULES\n[]\n-->"
        wf = _make_workflow(plan)
        result = parse_modules(wf)
        assert len(result) == 1
        assert result[0].id == "main"

    def test_empty_plan_output_returns_default(self) -> None:
        wf = _make_workflow("")
        result = parse_modules(wf)
        assert len(result) == 1
        assert result[0].id == "main"

    def test_modules_without_optional_fields(self) -> None:
        plan = '<!-- MODULES\n[{"id": "core"}]\n-->'
        wf = _make_workflow(plan)
        result = parse_modules(wf)
        assert len(result) == 1
        assert result[0].id == "core"
        assert result[0].name == "core"
        assert result[0].files == []
        assert result[0].deps == []

class TestExtractPlanOutput:

    def test_extracts_response_from_plan_stage(self) -> None:
        wf = _make_workflow("Hello plan output")
        assert extract_plan_output(wf) == "Hello plan output"

    def test_no_plan_stage_returns_empty(self) -> None:
        stage = MagicMock(spec=StageExecution)
        stage.ref_id = "implement"
        stage.outputs = {"response": "code"}

        wf = MagicMock(spec=Workflow)
        wf.stages = [stage]
        assert extract_plan_output(wf) == ""

    def test_plan_stage_no_outputs_returns_empty(self) -> None:
        stage = MagicMock(spec=StageExecution)
        stage.ref_id = "plan"
        stage.outputs = None

        wf = MagicMock(spec=Workflow)
        wf.stages = [stage]
        assert extract_plan_output(wf) == ""

    def test_plan_stage_uses_result_key_fallback(self) -> None:
        stage = MagicMock(spec=StageExecution)
        stage.ref_id = "plan"
        stage.outputs = {"result": "fallback output"}

        wf = MagicMock(spec=Workflow)
        wf.stages = [stage]
        assert extract_plan_output(wf) == "fallback output"

class TestStripPlanStage:

    def test_removes_plan_stage(self) -> None:
        plan = StageExecution(
            ref_id="plan",
            type="agent",
            name="Plan",
            context={},
            requisite_stage_ref_ids=set(),
            tasks=[
                TaskExecution.create(
                    name="Plan",
                    implementing_class="agent",
                    stage_start=True,
                    stage_end=True,
                )
            ],
        )
        impl = StageExecution(
            ref_id="implement",
            type="agent",
            name="Implement",
            context={},
            requisite_stage_ref_ids={"plan"},
            tasks=[
                TaskExecution.create(
                    name="Code",
                    implementing_class="agent",
                    stage_start=True,
                    stage_end=True,
                )
            ],
        )
        result = strip_plan_stage([plan, impl], "plan text")
        assert len(result) == 1
        assert result[0].ref_id == "implement"
        assert result[0].requisite_stage_ref_ids == set()
        assert result[0].context["ancestor_outputs"] == {"plan": "plan text"}
