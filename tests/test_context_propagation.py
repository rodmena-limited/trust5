"""Tests for context propagation in parallel and serial pipelines.

Verifies that plan_output, plan_config, and ancestor_outputs are correctly
injected into ALL stages that need them — especially integration_validate,
integration_repair, quality, and per-module repair stages.

Bug references: C1, C2, H9 from production-readiness audit.
"""

from __future__ import annotations

from trust5.workflows.parallel_pipeline import create_parallel_develop_workflow
from trust5.workflows.module_spec import ModuleSpec


def _make_modules() -> list[ModuleSpec]:
    """Create a minimal two-module spec for testing."""
    return [
        ModuleSpec(
            id="core",
            name="Core",
            files=["core.py"],
            test_files=["tests/test_core.py"],
            deps=[],
        ),
        ModuleSpec(
            id="api",
            name="API",
            files=["api.py"],
            test_files=["tests/test_api.py"],
            deps=["core"],
        ),
    ]


FAKE_PLAN_OUTPUT = "## SPEC: Test Plan\n- Module: Core\n- Module: API\n"
FAKE_PLAN_CONFIG = {"test_command": "pytest -v", "lint_command": "ruff check ."}


def _find_stage(workflow, ref_id: str):
    """Find a stage in a workflow by ref_id."""
    for stage in workflow.stages:
        if stage.ref_id == ref_id:
            return stage
    raise ValueError(f"Stage {ref_id!r} not found in workflow. Available: {[s.ref_id for s in workflow.stages]}")


# ---------------------------------------------------------------------------
# C1: integration_validate must have plan_output and ancestor_outputs
# ---------------------------------------------------------------------------


def test_integration_validate_has_plan_output(tmp_path, monkeypatch):
    """integration_validate stage must include plan_output in its context."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')

    wf = create_parallel_develop_workflow(
        modules=_make_modules(),
        user_request="build a thing",
        plan_output=FAKE_PLAN_OUTPUT,
    )

    stage = _find_stage(wf, "integration_validate")
    assert "plan_output" in stage.context, (
        "integration_validate is missing plan_output — agents cannot see the original plan"
    )
    assert stage.context["plan_output"] == FAKE_PLAN_OUTPUT


def test_integration_validate_has_ancestor_outputs(tmp_path, monkeypatch):
    """integration_validate stage must include ancestor_outputs with plan."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')

    wf = create_parallel_develop_workflow(
        modules=_make_modules(),
        user_request="build a thing",
        plan_output=FAKE_PLAN_OUTPUT,
    )

    stage = _find_stage(wf, "integration_validate")
    assert "ancestor_outputs" in stage.context, "integration_validate is missing ancestor_outputs"
    assert stage.context["ancestor_outputs"] == {"plan": FAKE_PLAN_OUTPUT}


def test_integration_validate_has_plan_config(tmp_path, monkeypatch):
    """integration_validate stage must include plan_config when provided."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')

    wf = create_parallel_develop_workflow(
        modules=_make_modules(),
        user_request="build a thing",
        plan_output=FAKE_PLAN_OUTPUT,
        plan_config_dict=FAKE_PLAN_CONFIG,
    )

    stage = _find_stage(wf, "integration_validate")
    assert "plan_config" in stage.context
    assert stage.context["plan_config"] == FAKE_PLAN_CONFIG


# ---------------------------------------------------------------------------
# C1: integration_repair must have plan_output, ancestor_outputs, plan_config
# ---------------------------------------------------------------------------


def test_integration_repair_has_plan_output(tmp_path, monkeypatch):
    """integration_repair stage must include plan_output in its context."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')

    wf = create_parallel_develop_workflow(
        modules=_make_modules(),
        user_request="build a thing",
        plan_output=FAKE_PLAN_OUTPUT,
    )

    stage = _find_stage(wf, "integration_repair")
    assert "plan_output" in stage.context, (
        "integration_repair is missing plan_output — repair agent can't reference the plan"
    )
    assert stage.context["plan_output"] == FAKE_PLAN_OUTPUT


def test_integration_repair_has_ancestor_outputs(tmp_path, monkeypatch):
    """integration_repair stage must include ancestor_outputs with plan."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')

    wf = create_parallel_develop_workflow(
        modules=_make_modules(),
        user_request="build a thing",
        plan_output=FAKE_PLAN_OUTPUT,
    )

    stage = _find_stage(wf, "integration_repair")
    assert "ancestor_outputs" in stage.context
    assert stage.context["ancestor_outputs"] == {"plan": FAKE_PLAN_OUTPUT}


def test_integration_repair_has_plan_config(tmp_path, monkeypatch):
    """integration_repair stage must include plan_config when provided."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')

    wf = create_parallel_develop_workflow(
        modules=_make_modules(),
        user_request="build a thing",
        plan_output=FAKE_PLAN_OUTPUT,
        plan_config_dict=FAKE_PLAN_CONFIG,
    )

    stage = _find_stage(wf, "integration_repair")
    assert "plan_config" in stage.context, (
        "integration_repair is missing plan_config — repair agent can't access test/lint commands"
    )
    assert stage.context["plan_config"] == FAKE_PLAN_CONFIG


# ---------------------------------------------------------------------------
# H9: quality stage must have plan_output and ancestor_outputs
# ---------------------------------------------------------------------------


def test_quality_has_plan_output(tmp_path, monkeypatch):
    """quality stage must include plan_output in its context."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')

    wf = create_parallel_develop_workflow(
        modules=_make_modules(),
        user_request="build a thing",
        plan_output=FAKE_PLAN_OUTPUT,
    )

    stage = _find_stage(wf, "quality")
    assert "plan_output" in stage.context, (
        "quality stage is missing plan_output — quality gate can't verify implementation matches plan"
    )
    assert stage.context["plan_output"] == FAKE_PLAN_OUTPUT


def test_quality_has_ancestor_outputs(tmp_path, monkeypatch):
    """quality stage must include ancestor_outputs with plan."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')

    wf = create_parallel_develop_workflow(
        modules=_make_modules(),
        user_request="build a thing",
        plan_output=FAKE_PLAN_OUTPUT,
    )

    stage = _find_stage(wf, "quality")
    assert "ancestor_outputs" in stage.context
    assert stage.context["ancestor_outputs"] == {"plan": FAKE_PLAN_OUTPUT}


# ---------------------------------------------------------------------------
# Per-module repair stage must have plan_output and plan_config
# ---------------------------------------------------------------------------


def test_per_module_repair_has_plan_output(tmp_path, monkeypatch):
    """Per-module repair stages must include plan_output."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')

    wf = create_parallel_develop_workflow(
        modules=_make_modules(),
        user_request="build a thing",
        plan_output=FAKE_PLAN_OUTPUT,
    )

    # Find any repair_* stage (per-module)
    repair_stages = [s for s in wf.stages if s.ref_id.startswith("repair_")]
    assert repair_stages, "Expected at least one per-module repair stage"

    for stage in repair_stages:
        assert "plan_output" in stage.context, f"Per-module repair stage {stage.ref_id!r} is missing plan_output"
        assert stage.context["plan_output"] == FAKE_PLAN_OUTPUT


def test_per_module_repair_has_ancestor_outputs(tmp_path, monkeypatch):
    """Per-module repair stages must include ancestor_outputs."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')

    wf = create_parallel_develop_workflow(
        modules=_make_modules(),
        user_request="build a thing",
        plan_output=FAKE_PLAN_OUTPUT,
    )

    repair_stages = [s for s in wf.stages if s.ref_id.startswith("repair_")]
    for stage in repair_stages:
        assert "ancestor_outputs" in stage.context, (
            f"Per-module repair stage {stage.ref_id!r} is missing ancestor_outputs"
        )
        assert stage.context["ancestor_outputs"] == {"plan": FAKE_PLAN_OUTPUT}


def test_per_module_repair_has_plan_config(tmp_path, monkeypatch):
    """Per-module repair stages must include plan_config when provided."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')

    wf = create_parallel_develop_workflow(
        modules=_make_modules(),
        user_request="build a thing",
        plan_output=FAKE_PLAN_OUTPUT,
        plan_config_dict=FAKE_PLAN_CONFIG,
    )

    repair_stages = [s for s in wf.stages if s.ref_id.startswith("repair_")]
    for stage in repair_stages:
        assert "plan_config" in stage.context, f"Per-module repair stage {stage.ref_id!r} is missing plan_config"
        assert stage.context["plan_config"] == FAKE_PLAN_CONFIG


# ---------------------------------------------------------------------------
# Validate existing stages still have plan_output (regression guard)
# ---------------------------------------------------------------------------


def test_write_tests_stages_have_plan_output(tmp_path, monkeypatch):
    """Write tests stages must have plan_output (regression guard)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')

    wf = create_parallel_develop_workflow(
        modules=_make_modules(),
        user_request="build a thing",
        plan_output=FAKE_PLAN_OUTPUT,
    )

    wt_stages = [s for s in wf.stages if s.ref_id.startswith("write_tests_")]
    assert wt_stages, "Expected write_tests stages"
    for stage in wt_stages:
        assert stage.context.get("plan_output") == FAKE_PLAN_OUTPUT


def test_implement_stages_have_plan_output(tmp_path, monkeypatch):
    """Implement stages must have plan_output (regression guard)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')

    wf = create_parallel_develop_workflow(
        modules=_make_modules(),
        user_request="build a thing",
        plan_output=FAKE_PLAN_OUTPUT,
    )

    impl_stages = [s for s in wf.stages if s.ref_id.startswith("implement_")]
    assert impl_stages, "Expected implement stages"
    for stage in impl_stages:
        assert stage.context.get("plan_output") == FAKE_PLAN_OUTPUT


# ---------------------------------------------------------------------------
# No plan_config leakage when not provided
# ---------------------------------------------------------------------------


def test_no_plan_config_when_not_provided(tmp_path, monkeypatch):
    """When plan_config_dict is None, no stage should have plan_config."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')

    wf = create_parallel_develop_workflow(
        modules=_make_modules(),
        user_request="build a thing",
        plan_output=FAKE_PLAN_OUTPUT,
        plan_config_dict=None,
    )

    for stage in wf.stages:
        if stage.ref_id in ("setup", "watchdog"):
            continue
        assert "plan_config" not in stage.context, (
            f"Stage {stage.ref_id!r} has plan_config but plan_config_dict was None"
        )
