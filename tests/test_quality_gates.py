"""Tests for trust5.core.quality_gates — phase-specific quality gate validation."""

from __future__ import annotations

from trust5.core.quality_gates import DiagnosticSnapshot, validate_plan_phase


def test_plan_phase_reports_baseline_errors():
    """validate_plan_phase reports pre-existing errors as warnings."""
    snapshot = DiagnosticSnapshot(errors=5, type_errors=2)
    issues = validate_plan_phase(snapshot)
    assert len(issues) == 2
    assert issues[0].severity == "warning"
    assert "5 pre-existing errors" in issues[0].message
    assert issues[0].rule == "phase-plan-baseline"
    assert issues[1].severity == "warning"
    assert "2 pre-existing type errors" in issues[1].message
    assert issues[1].rule == "phase-plan-type-baseline"


def test_plan_phase_clean_returns_empty():
    """validate_plan_phase returns no issues when baseline is clean."""
    snapshot = DiagnosticSnapshot(errors=0, type_errors=0)
    issues = validate_plan_phase(snapshot)
    assert issues == []
