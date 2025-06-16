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

    def test_empty_plan_output_no_ancestor(self) -> None:
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
        result = strip_plan_stage([impl], "")
        assert "ancestor_outputs" not in result[0].context

class TestCreateParallelDevelopWorkflow:

    def test_single_module_creates_correct_stages(self) -> None:
        modules = [
            ModuleSpec(
                id="core",
                name="Core",
                files=["src/core.py"],
                test_files=["tests/test_core.py"],
            )
        ]
        wf = create_parallel_develop_workflow(modules, "Build a CLI", "plan text")
        ref_ids = [s.ref_id for s in wf.stages]

        assert "implement_core" in ref_ids
        assert "validate_core" in ref_ids
        assert "repair_core" in ref_ids
        assert "integration_validate" in ref_ids
        assert "integration_repair" in ref_ids
        assert "quality" in ref_ids

    def test_two_modules_with_dependency(self) -> None:
        modules = [
            ModuleSpec(id="auth", name="Auth", files=["src/auth.py"]),
            ModuleSpec(id="api", name="API", files=["src/api.py"], deps=["auth"]),
        ]
        wf = create_parallel_develop_workflow(modules, "req", "plan")
        stage_map = {s.ref_id: s for s in wf.stages}

        wt_deps = stage_map.get(
            "write_tests_api",
            MagicMock(requisite_stage_ref_ids=set()),
        ).requisite_stage_ref_ids
        impl_deps = stage_map["implement_api"].requisite_stage_ref_ids
        assert "validate_auth" in impl_deps or "validate_auth" in wt_deps

    def test_integration_validate_depends_on_all_module_validates(self) -> None:
        modules = [
            ModuleSpec(id="a", name="A"),
            ModuleSpec(id="b", name="B"),
            ModuleSpec(id="c", name="C"),
        ]
        wf = create_parallel_develop_workflow(modules, "req", "plan")
        stage_map = {s.ref_id: s for s in wf.stages}

        int_val = stage_map["integration_validate"]
        assert "validate_a" in int_val.requisite_stage_ref_ids
        assert "validate_b" in int_val.requisite_stage_ref_ids
        assert "validate_c" in int_val.requisite_stage_ref_ids

    def test_module_context_propagated_to_validate(self) -> None:
        modules = [
            ModuleSpec(
                id="db",
                name="Database",
                files=["src/db.py"],
                test_files=["tests/test_db.py"],
            )
        ]
        wf = create_parallel_develop_workflow(modules, "req", "plan")
        stage_map = {s.ref_id: s for s in wf.stages}

        val = stage_map["validate_db"]
        assert val.context["jump_repair_ref"] == "repair_db"
        assert val.context["jump_validate_ref"] == "validate_db"
        assert val.context["jump_implement_ref"] == "implement_db"
        assert val.context["owned_files"] == ["src/db.py"]
        assert val.context["test_files"] == ["tests/test_db.py"]
        assert val.context["module_name"] == "Database"

    def test_repair_stage_has_module_context(self) -> None:
        modules = [ModuleSpec(id="x", name="X")]
        wf = create_parallel_develop_workflow(modules, "req", "plan")
        stage_map = {s.ref_id: s for s in wf.stages}

        rep = stage_map["repair_x"]
        assert rep.context["jump_repair_ref"] == "repair_x"
        assert rep.context["jump_validate_ref"] == "validate_x"

    def test_quality_stage_jumps_to_integration_repair(self) -> None:
        modules = [ModuleSpec(id="m", name="M")]
        wf = create_parallel_develop_workflow(modules, "req", "plan")
        stage_map = {s.ref_id: s for s in wf.stages}

        quality = stage_map["quality"]
        assert quality.context["jump_repair_ref"] == "integration_repair"

    def test_workflow_name(self) -> None:
        modules = [ModuleSpec(id="m", name="M")]
        wf = create_parallel_develop_workflow(modules, "req", "plan")
        assert wf.name == "Parallel Develop Pipeline"

class TestModuleSpec:
    pass
