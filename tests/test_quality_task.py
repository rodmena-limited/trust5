from __future__ import annotations
from unittest.mock import MagicMock, patch
from stabilize.models.status import WorkflowStatus
from trust5.core.config import QualityConfig
from trust5.core.quality import (
    PrincipleResult,
    QualityReport,
    _filter_excluded_findings,
    _path_in_skip_dirs,
)
from trust5.tasks.quality_task import QualityTask

def make_stage(context: dict | None = None) -> MagicMock:
    stage = MagicMock()
    stage.context = context or {}
    stage.context.setdefault("project_root", "/tmp/fake-project")
    return stage

def _make_report(score: float = 0.90, passed: bool = True, errors: int = 0, warnings: int = 0) -> QualityReport:
    """Create a QualityReport with reasonable defaults."""
    return QualityReport(
        passed=passed,
        score=score,
        principles={
            "tested": PrincipleResult(name="tested", passed=True, score=1.0),
            "readable": PrincipleResult(name="readable", passed=True, score=1.0),
            "understandable": PrincipleResult(name="understandable", passed=True, score=1.0),
            "secured": PrincipleResult(name="secured", passed=True, score=1.0),
            "trackable": PrincipleResult(name="trackable", passed=True, score=1.0),
        },
        total_errors=errors,
        total_warnings=warnings,
        coverage_pct=90.0,
        timestamp="2026-02-15T12:00:00+00:00",
    )

def _make_failing_report(score: float = 0.50) -> QualityReport:
    """Create a failing QualityReport."""
    return QualityReport(
        passed=False,
        score=score,
        principles={
            "tested": PrincipleResult(name="tested", passed=False, score=0.3),
            "readable": PrincipleResult(name="readable", passed=True, score=0.8),
            "understandable": PrincipleResult(name="understandable", passed=True, score=0.7),
            "secured": PrincipleResult(name="secured", passed=True, score=0.9),
            "trackable": PrincipleResult(name="trackable", passed=True, score=0.8),
        },
        total_errors=3,
        total_warnings=2,
        coverage_pct=40.0,
        timestamp="2026-02-15T12:00:00+00:00",
    )

def test_quality_passes(
    mock_meets,
    mock_snapshot,
    mock_methodology,
    mock_phase,
    mock_config_mgr,
    mock_gate_cls,
    mock_emit_block,
    mock_emit,
):
    """High quality score passes the gate and returns success."""
    report = _make_report(score=0.90, passed=True)
    mock_gate = MagicMock()
    mock_gate.validate.return_value = report
    mock_gate_cls.return_value = mock_gate

    config = QualityConfig(enforce_quality=True, pass_score_threshold=0.70)
    mock_mgr_inst = MagicMock()
    mock_mgr_inst.load_config.return_value = MagicMock(quality=config)
    mock_config_mgr.return_value = mock_mgr_inst

    mock_snapshot.return_value = MagicMock(
        errors=0,
        warnings=0,
        type_errors=0,
        lint_errors=0,
        security_warnings=0,
        timestamp="",
    )

    task = QualityTask()
    stage = make_stage({"quality_attempt": 0})

    result = task.execute(stage)

    assert result.status == WorkflowStatus.SUCCEEDED
    assert result.outputs["quality_passed"] is True
    assert result.outputs["quality_score"] == 0.90

def test_quality_fails_jumps_to_repair(
    mock_propagate,
    mock_stagnant,
    mock_meets,
    mock_snapshot,
    mock_methodology,
    mock_phase,
    mock_config_mgr,
    mock_gate_cls,
    mock_emit_block,
    mock_emit,
):
    """Low quality score triggers jump to repair."""
    report = _make_failing_report(score=0.50)
    mock_gate = MagicMock()
    mock_gate.validate.return_value = report
    mock_gate_cls.return_value = mock_gate

    config = QualityConfig(enforce_quality=True, pass_score_threshold=0.70)
    mock_mgr_inst = MagicMock()
    mock_mgr_inst.load_config.return_value = MagicMock(quality=config)
    mock_config_mgr.return_value = mock_mgr_inst

    mock_snapshot.return_value = MagicMock(
        errors=3,
        warnings=2,
        type_errors=0,
        lint_errors=0,
        security_warnings=0,
        timestamp="",
    )

    task = QualityTask()
    stage = make_stage({"quality_attempt": 0, "max_quality_attempts": 3})

    result = task.execute(stage)

    assert result.status == WorkflowStatus.REDIRECT
    assert result.target_stage_ref_id == "repair"
    assert result.context["failure_type"] == "quality"
    assert result.context["_repair_requested"] is True
    assert result.context["quality_attempt"] == 1

def test_quality_max_attempts_accepts_partial(
    mock_meets,
    mock_snapshot,
    mock_methodology,
    mock_phase,
    mock_config_mgr,
    mock_gate_cls,
    mock_emit_block,
    mock_emit,
):
    """At max quality attempts, accept partial result with failed_continue."""
    report = _make_failing_report(score=0.55)
    mock_gate = MagicMock()
    mock_gate.validate.return_value = report
    mock_gate_cls.return_value = mock_gate

    config = QualityConfig(enforce_quality=True, pass_score_threshold=0.70)
    mock_mgr_inst = MagicMock()
    mock_mgr_inst.load_config.return_value = MagicMock(quality=config)
    mock_config_mgr.return_value = mock_mgr_inst

    mock_snapshot.return_value = MagicMock(
        errors=2,
        warnings=1,
        type_errors=0,
        lint_errors=0,
        security_warnings=0,
        timestamp="",
    )

    task = QualityTask()
    stage = make_stage({"quality_attempt": 3, "max_quality_attempts": 3})

    result = task.execute(stage)

    assert result.status == WorkflowStatus.FAILED_CONTINUE
    assert result.outputs["quality_passed"] is False
    assert result.outputs["quality_attempts_used"] == 3

def test_quality_stagnant_accepts_partial(
    mock_stagnant,
    mock_meets,
    mock_snapshot,
    mock_methodology,
    mock_phase,
    mock_config_mgr,
    mock_gate_cls,
    mock_emit_block,
    mock_emit,
):
    """When quality is stagnant (no improvement), accept partial."""
    report = _make_failing_report(score=0.55)
    mock_gate = MagicMock()
    mock_gate.validate.return_value = report
    mock_gate_cls.return_value = mock_gate

    config = QualityConfig(enforce_quality=True, pass_score_threshold=0.70)
    mock_mgr_inst = MagicMock()
    mock_mgr_inst.load_config.return_value = MagicMock(quality=config)
    mock_config_mgr.return_value = mock_mgr_inst

    mock_snapshot.return_value = MagicMock(
        errors=2,
        warnings=1,
        type_errors=0,
        lint_errors=0,
        security_warnings=0,
        timestamp="",
    )

    task = QualityTask()
    stage = make_stage(
        {
            "quality_attempt": 1,
            "max_quality_attempts": 3,
            "prev_quality_report": {"score": 0.55, "total_errors": 3, "total_warnings": 1},
        }
    )

    result = task.execute(stage)

    assert result.status == WorkflowStatus.FAILED_CONTINUE
    assert result.outputs["quality_passed"] is False

def test_quality_threshold_clamped_low(
    mock_meets,
    mock_snapshot,
    mock_methodology,
    mock_phase,
    mock_config_mgr,
    mock_gate_cls,
    mock_emit_block,
    mock_emit,
):
    """plan_config threshold of 0.0 is clamped to 0.1."""
    report = _make_report(score=0.90)
    mock_gate = MagicMock()
    mock_gate.validate.return_value = report
    mock_gate_cls.return_value = mock_gate

    config = QualityConfig(enforce_quality=True, pass_score_threshold=0.70)
    mock_mgr_inst = MagicMock()
    mock_mgr_inst.load_config.return_value = MagicMock(quality=config)
    mock_config_mgr.return_value = mock_mgr_inst

    mock_snapshot.return_value = MagicMock(
        errors=0,
        warnings=0,
        type_errors=0,
        lint_errors=0,
        security_warnings=0,
        timestamp="",
    )

    task = QualityTask()
    stage = make_stage(
        {
            "quality_attempt": 0,
            "plan_config": {"quality_threshold": 0.0},
        }
    )

    task.execute(stage)

    # After execute, the config's pass_score_threshold should be clamped to 0.1
    assert config.pass_score_threshold == 0.1

def test_quality_threshold_clamped_high(
    mock_meets,
    mock_snapshot,
    mock_methodology,
    mock_phase,
    mock_config_mgr,
    mock_gate_cls,
    mock_emit_block,
    mock_emit,
):
    """plan_config threshold of 2.0 is clamped to 1.0."""
    report = _make_report(score=0.90)
    mock_gate = MagicMock()
    mock_gate.validate.return_value = report
    mock_gate_cls.return_value = mock_gate

    config = QualityConfig(enforce_quality=True, pass_score_threshold=0.70)
    mock_mgr_inst = MagicMock()
    mock_mgr_inst.load_config.return_value = MagicMock(quality=config)
    mock_config_mgr.return_value = mock_mgr_inst

    mock_snapshot.return_value = MagicMock(
        errors=0,
        warnings=0,
        type_errors=0,
        lint_errors=0,
        security_warnings=0,
        timestamp="",
    )

    task = QualityTask()
    stage = make_stage(
        {
            "quality_attempt": 0,
            "plan_config": {"quality_threshold": 2.0},
        }
    )

    task.execute(stage)

    assert config.pass_score_threshold == 1.0

def test_quality_skipped_when_disabled(mock_config_mgr, mock_emit_block, mock_emit):
    """When enforce_quality=False, quality gate is skipped."""
    config = QualityConfig(enforce_quality=False)
    mock_mgr_inst = MagicMock()
    mock_mgr_inst.load_config.return_value = MagicMock(quality=config)
    mock_config_mgr.return_value = mock_mgr_inst

    task = QualityTask()
    stage = make_stage()

    result = task.execute(stage)

    assert result.status == WorkflowStatus.SUCCEEDED
    assert result.outputs["quality_passed"] is True
    assert result.outputs["quality_skipped"] is True

def test_quality_tests_partial_accepts(
    mock_meets,
    mock_snapshot,
    mock_methodology,
    mock_phase,
    mock_config_mgr,
    mock_gate_cls,
    mock_emit_block,
    mock_emit,
):
    """When tests_partial=True, accept partial result immediately (no repair loop)."""
    report = _make_failing_report(score=0.50)
    mock_gate = MagicMock()
    mock_gate.validate.return_value = report
    mock_gate_cls.return_value = mock_gate

    config = QualityConfig(enforce_quality=True, pass_score_threshold=0.70)
    mock_mgr_inst = MagicMock()
    mock_mgr_inst.load_config.return_value = MagicMock(quality=config)
    mock_config_mgr.return_value = mock_mgr_inst

    mock_snapshot.return_value = MagicMock(
        errors=3,
        warnings=2,
        type_errors=0,
        lint_errors=0,
        security_warnings=0,
        timestamp="",
    )

    task = QualityTask()
    stage = make_stage(
        {
            "quality_attempt": 0,
            "max_quality_attempts": 3,
            "tests_partial": True,
        }
    )

    result = task.execute(stage)

    assert result.status == WorkflowStatus.FAILED_CONTINUE
    assert result.outputs["quality_passed"] is False
    assert result.outputs.get("tests_partial") is True
    # Crucially, it should NOT jump to repair (no REDIRECT)
    assert result.target_stage_ref_id is None

def test_path_in_skip_dirs_venv():
    assert _path_in_skip_dirs("./venv/lib/python3.14/site-packages/PIL/foo.py", {"venv", ".venv"})
