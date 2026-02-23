"""Tests for trust5/core/runner.py — check_stage_failures(), finalize_status(), wait_for_completion()."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from stabilize.models.status import WorkflowStatus

from trust5.core.runner import check_stage_failures, finalize_status, wait_for_completion


def make_stage(ref_id: str, status: WorkflowStatus, outputs: dict | None = None, error: str | None = None):
    stage = MagicMock()
    stage.ref_id = ref_id
    stage.status = status
    stage.outputs = outputs or {}
    stage.error = error or ""
    return stage


def make_workflow(status: WorkflowStatus, stages: list) -> MagicMock:
    workflow = MagicMock()
    workflow.status = status
    workflow.stages = stages
    return workflow


# ── check_stage_failures tests ──


def test_check_stage_failures_detects_test_failure():
    """FAILED_CONTINUE stage with tests_passed=False is detected."""
    stages = [
        make_stage("setup", WorkflowStatus.SUCCEEDED),
        make_stage("validate", WorkflowStatus.FAILED_CONTINUE, {"tests_passed": False, "repair_attempts_used": 5}),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)

    has_test, has_quality, has_compliance, details = check_stage_failures(workflow)

    assert has_test is True
    assert has_quality is False
    assert len(details) >= 1
    assert "tests failing" in details[0].lower()


def test_check_stage_failures_detects_quality_failure():
    """FAILED_CONTINUE stage with quality_passed=False is detected."""
    stages = [
        make_stage("setup", WorkflowStatus.SUCCEEDED),
        make_stage("quality", WorkflowStatus.FAILED_CONTINUE, {"quality_passed": False, "quality_score": 0.55}),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)

    has_test, has_quality, has_compliance, details = check_stage_failures(workflow)

    assert has_test is False
    assert has_quality is True
    assert len(details) >= 1
    assert "quality" in details[0].lower()


def test_check_stage_failures_detects_terminal_test_failure():
    """TERMINAL stage with 'tests still failing' in error is detected."""
    stages = [
        make_stage(
            "validate",
            WorkflowStatus.TERMINAL,
            outputs={},
            error="Tests still failing after 3 reimplementations x 5 repairs",
        ),
    ]
    workflow = make_workflow(WorkflowStatus.TERMINAL, stages)

    has_test, has_quality, has_compliance, details = check_stage_failures(workflow)

    assert has_test is True
    assert len(details) >= 1


def test_check_stage_failures_detects_reimplementation_error():
    """TERMINAL stage with 'reimplementation' in error is detected."""
    stages = [
        make_stage(
            "validate",
            WorkflowStatus.TERMINAL,
            outputs={},
            error="All reimplementation attempts exhausted",
        ),
    ]
    workflow = make_workflow(WorkflowStatus.TERMINAL, stages)

    has_test, has_quality, has_compliance, details = check_stage_failures(workflow)

    assert has_test is True


def test_check_stage_failures_ignores_succeeded():
    """SUCCEEDED stages are skipped entirely."""
    stages = [
        make_stage("setup", WorkflowStatus.SUCCEEDED, {"tests_passed": True}),
        make_stage("validate", WorkflowStatus.SUCCEEDED, {"tests_passed": True}),
        make_stage("quality", WorkflowStatus.SUCCEEDED, {"quality_passed": True, "quality_score": 0.92}),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)

    has_test, has_quality, has_compliance, details = check_stage_failures(workflow)

    assert has_test is False
    assert has_quality is False
    assert len(details) == 0


def test_check_stage_failures_detects_tests_partial():
    """FAILED_CONTINUE stage with tests_partial=True is a test failure."""
    stages = [
        make_stage("validate", WorkflowStatus.FAILED_CONTINUE, {"tests_partial": True}),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)

    has_test, has_quality, has_compliance, details = check_stage_failures(workflow)

    assert has_test is True


# ── finalize_status tests ──


@patch("trust5.core.runner.emit")
def test_finalize_status_overrides_to_terminal(mock_emit):
    """SUCCEEDED workflow with test failures is overridden to TERMINAL."""
    stages = [
        make_stage("setup", WorkflowStatus.SUCCEEDED),
        make_stage("validate", WorkflowStatus.FAILED_CONTINUE, {"tests_passed": False, "repair_attempts_used": 5}),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)
    store = MagicMock()

    finalize_status(workflow, store)

    assert workflow.status == WorkflowStatus.TERMINAL
    store.update_status.assert_called_once_with(workflow)
    # Verify WFAL messages were emitted
    emit_calls = [c for c in mock_emit.call_args_list if c[0][0].value == "WFAL"]
    assert len(emit_calls) >= 1


@patch("trust5.core.runner.emit")
def test_finalize_status_keeps_succeeded_with_quality_warning(mock_emit):
    """Quality fail only keeps SUCCEEDED but emits warnings."""
    stages = [
        make_stage("validate", WorkflowStatus.SUCCEEDED, {"tests_passed": True}),
        make_stage("quality", WorkflowStatus.FAILED_CONTINUE, {"quality_passed": False, "quality_score": 0.60}),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)
    store = MagicMock()

    finalize_status(workflow, store)

    # Status should remain SUCCEEDED (not overridden)
    assert workflow.status == WorkflowStatus.SUCCEEDED
    store.update_status.assert_not_called()
    # Should emit WSUC (success with warnings) and SWRN
    wsuc_calls = [c for c in mock_emit.call_args_list if c[0][0].value == "WSUC"]
    swrn_calls = [c for c in mock_emit.call_args_list if c[0][0].value == "SWRN"]
    assert len(wsuc_calls) >= 1
    assert len(swrn_calls) >= 1


@patch("trust5.core.runner.emit")
def test_finalize_status_clean_succeeded(mock_emit):
    """Clean SUCCEEDED workflow emits simple success."""
    stages = [
        make_stage("setup", WorkflowStatus.SUCCEEDED),
        make_stage("validate", WorkflowStatus.SUCCEEDED, {"tests_passed": True}),
        make_stage("quality", WorkflowStatus.SUCCEEDED, {"quality_passed": True}),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)
    store = MagicMock()

    finalize_status(workflow, store)

    assert workflow.status == WorkflowStatus.SUCCEEDED
    store.update_status.assert_not_called()
    wsuc_calls = [c for c in mock_emit.call_args_list if c[0][0].value == "WSUC"]
    assert len(wsuc_calls) == 1
    assert "SUCCEEDED" in wsuc_calls[0][0][1]


@patch("trust5.core.runner.emit")
def test_finalize_status_failed_continue_workflow(mock_emit):
    """FAILED_CONTINUE workflow status emits WFAL."""
    workflow = make_workflow(WorkflowStatus.FAILED_CONTINUE, [])
    store = MagicMock()

    finalize_status(workflow, store)

    wfal_calls = [c for c in mock_emit.call_args_list if c[0][0].value == "WFAL"]
    assert len(wfal_calls) >= 1
    assert "incomplete" in wfal_calls[0][0][1].lower()


@patch("trust5.core.runner.emit")
def test_finalize_status_terminal_workflow(mock_emit):
    """TERMINAL workflow status emits WFAL with status name."""
    workflow = make_workflow(WorkflowStatus.TERMINAL, [])
    store = MagicMock()

    finalize_status(workflow, store)

    wfal_calls = [c for c in mock_emit.call_args_list if c[0][0].value == "WFAL"]
    assert len(wfal_calls) >= 1
    assert "TERMINAL" in wfal_calls[0][0][1]


@patch("trust5.core.runner.emit")
def test_finalize_status_both_test_and_quality_failures(mock_emit):
    """Both test and quality failures: TERMINAL override with combined message."""
    stages = [
        make_stage("validate", WorkflowStatus.FAILED_CONTINUE, {"tests_passed": False}),
        make_stage("quality", WorkflowStatus.FAILED_CONTINUE, {"quality_passed": False, "quality_score": 0.4}),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)
    store = MagicMock()

    finalize_status(workflow, store)

    assert workflow.status == WorkflowStatus.TERMINAL
    store.update_status.assert_called_once()
    # Both problems should be mentioned
    wfal_calls = [c for c in mock_emit.call_args_list if c[0][0].value == "WFAL"]
    failure_msg = wfal_calls[0][0][1]
    assert "tests failing" in failure_msg
    assert "quality" in failure_msg


# ── wait_for_completion tests ──


def test_wait_for_completion_returns_on_terminal():
    """wait_for_completion returns immediately when workflow is already terminal."""
    store = MagicMock()
    wf = MagicMock()
    wf.status = WorkflowStatus.SUCCEEDED
    store.retrieve.return_value = wf

    result = wait_for_completion(store, "wf-123", timeout=10.0)

    assert result.status == WorkflowStatus.SUCCEEDED


def test_wait_for_completion_returns_early_on_stop_event():
    """When stop_event is set, wait_for_completion exits immediately."""
    store = MagicMock()
    wf = MagicMock()
    wf.status = WorkflowStatus.RUNNING
    store.retrieve.return_value = wf

    stop = threading.Event()
    stop.set()  # Already signaled — should return on first iteration

    result = wait_for_completion(store, "wf-123", timeout=600.0, stop_event=stop)

    assert result.status == WorkflowStatus.RUNNING
    # Should have called retrieve exactly once (early exit, no polling)
    assert store.retrieve.call_count == 1


def test_wait_for_completion_ignores_none_stop_event():
    """When stop_event=None (default), the function polls normally."""
    store = MagicMock()
    wf = MagicMock()
    wf.status = WorkflowStatus.SUCCEEDED
    store.retrieve.return_value = wf

    result = wait_for_completion(store, "wf-123", timeout=10.0, stop_event=None)

    assert result.status == WorkflowStatus.SUCCEEDED


# ── Progressive polling tests ──────────────────────────────────────


def test_progressive_poll_constants_exist():
    """Progressive polling constants are defined and ordered correctly."""
    from trust5.core.runner import POLL_INTERVAL, POLL_INTERVAL_FAST, POLL_INTERVAL_MODERATE, POLL_INTERVAL_SLOW

    assert POLL_INTERVAL_FAST == 0.5
    assert POLL_INTERVAL_MODERATE == 2.0
    assert POLL_INTERVAL_SLOW == 5.0
    assert POLL_INTERVAL == POLL_INTERVAL_FAST  # backward compat


def test_progressive_poll_constants_ordered():
    """Fast < Moderate < Slow."""
    from trust5.core.runner import POLL_INTERVAL_FAST, POLL_INTERVAL_MODERATE, POLL_INTERVAL_SLOW

    assert POLL_INTERVAL_FAST < POLL_INTERVAL_MODERATE < POLL_INTERVAL_SLOW


@patch("trust5.core.runner.time")
def test_wait_for_completion_uses_fast_poll_initially(mock_time):
    """Within first 60s, polling uses POLL_INTERVAL_FAST."""
    store = MagicMock()

    # First call: RUNNING, second call: SUCCEEDED
    wf_running = MagicMock()
    wf_running.status = WorkflowStatus.RUNNING
    wf_done = MagicMock()
    wf_done.status = WorkflowStatus.SUCCEEDED

    store.retrieve.side_effect = [wf_running, wf_done]

    # Simulate: monotonic() returns 100, 100 (start), 100+0.5 (loop check < deadline), 100 (elapsed calc)
    # Keep elapsed < 60 to stay in FAST interval
    mock_time.monotonic.side_effect = [100.0, 100.0, 100.5, 100.0, 100.5, 101.0]
    mock_time.sleep = MagicMock()

    from trust5.core.runner import POLL_INTERVAL_FAST

    result = wait_for_completion(store, "wf-test", timeout=600.0)

    assert result.status == WorkflowStatus.SUCCEEDED
    # Should have slept with FAST interval
    if mock_time.sleep.called:
        mock_time.sleep.assert_called_with(POLL_INTERVAL_FAST)
