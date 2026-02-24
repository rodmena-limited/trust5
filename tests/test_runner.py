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
    stages = [
        make_stage("setup", WorkflowStatus.SUCCEEDED),
        make_stage("validate", WorkflowStatus.FAILED_CONTINUE, {"tests_passed": False, "repair_attempts_used": 5}),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)

    has_test, has_quality, has_review, has_compliance, details = check_stage_failures(workflow)

    assert has_test is True
    assert has_quality is False
    assert len(details) >= 1
    assert "tests failing" in details[0].lower()


def test_check_stage_failures_detects_quality_failure():
    stages = [
        make_stage("setup", WorkflowStatus.SUCCEEDED),
        make_stage("quality", WorkflowStatus.FAILED_CONTINUE, {"quality_passed": False, "quality_score": 0.55}),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)

    has_test, has_quality, has_review, has_compliance, details = check_stage_failures(workflow)

    assert has_test is False
    assert has_quality is True
    assert len(details) >= 1
    assert "quality" in details[0].lower()


def test_check_stage_failures_detects_terminal_test_failure():
    stages = [
        make_stage(
            "validate",
            WorkflowStatus.TERMINAL,
            outputs={},
            error="Tests still failing after 3 reimplementations x 5 repairs",
        ),
    ]
    workflow = make_workflow(WorkflowStatus.TERMINAL, stages)

    has_test, has_quality, has_review, has_compliance, details = check_stage_failures(workflow)

    assert has_test is True
    assert len(details) >= 1


def test_check_stage_failures_detects_reimplementation_error():
    stages = [
        make_stage(
            "validate",
            WorkflowStatus.TERMINAL,
            outputs={},
            error="All reimplementation attempts exhausted",
        ),
    ]
    workflow = make_workflow(WorkflowStatus.TERMINAL, stages)

    has_test, has_quality, has_review, has_compliance, details = check_stage_failures(workflow)

    assert has_test is True


def test_check_stage_failures_ignores_succeeded():
    stages = [
        make_stage("setup", WorkflowStatus.SUCCEEDED, {"tests_passed": True}),
        make_stage("validate", WorkflowStatus.SUCCEEDED, {"tests_passed": True}),
        make_stage("quality", WorkflowStatus.SUCCEEDED, {"quality_passed": True, "quality_score": 0.92}),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)

    has_test, has_quality, has_review, has_compliance, details = check_stage_failures(workflow)

    assert has_test is False
    assert has_quality is False
    assert len(details) == 0


def test_check_stage_failures_detects_tests_partial():
    stages = [
        make_stage("validate", WorkflowStatus.FAILED_CONTINUE, {"tests_partial": True}),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)

    has_test, has_quality, has_review, has_compliance, details = check_stage_failures(workflow)

    assert has_test is True


def test_check_stage_failures_compliance_uses_output_flag():
    """compliance_passed=False in outputs triggers compliance failure (not a hardcoded ratio)."""
    stages = [
        make_stage(
            "quality",
            WorkflowStatus.FAILED_CONTINUE,
            {
                "quality_passed": False,
                "spec_compliance_ratio": 0.71,
                "compliance_passed": False,
                "spec_criteria_met": 12,
                "spec_criteria_total": 17,
                "spec_unmet_criteria": ["[UBIQ] Missing JSON content-type"],
            },
        ),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)

    _, _, _, has_compliance, details = check_stage_failures(workflow)

    assert has_compliance is True
    assert any("12/17" in d for d in details)


def test_check_stage_failures_compliance_passed_true_no_failure():
    """compliance_passed=True means no compliance failure even if ratio < 1.0."""
    stages = [
        make_stage(
            "quality",
            WorkflowStatus.SUCCEEDED,
            {
                "quality_passed": True,
                "spec_compliance_ratio": 0.71,
                "compliance_passed": True,
                "spec_criteria_met": 12,
                "spec_criteria_total": 17,
            },
        ),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)

    _, _, _, has_compliance, details = check_stage_failures(workflow)

    assert has_compliance is False
    assert any("12/17" in d for d in details)


def test_check_stage_failures_detects_review_failure():
    stages = [
        make_stage(
            "review",
            WorkflowStatus.FAILED_CONTINUE,
            {"review_passed": False, "review_score": 0.5},
        ),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)

    _, _, has_review, _, details = check_stage_failures(workflow)

    assert has_review is True
    assert any("review" in d.lower() for d in details)


# ── finalize_status tests ──


@patch("trust5.core.runner.emit")
def test_finalize_status_overrides_to_terminal_on_test_failure(mock_emit):
    stages = [
        make_stage("setup", WorkflowStatus.SUCCEEDED),
        make_stage("validate", WorkflowStatus.FAILED_CONTINUE, {"tests_passed": False, "repair_attempts_used": 5}),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)
    store = MagicMock()

    finalize_status(workflow, store)

    assert workflow.status == WorkflowStatus.TERMINAL
    store.update_status.assert_called_once_with(workflow)
    wfal_calls = [c for c in mock_emit.call_args_list if c[0][0].value == "WFAL"]
    assert len(wfal_calls) >= 1


@patch("trust5.core.runner.emit")
def test_finalize_status_overrides_to_terminal_on_quality_failure(mock_emit):
    """Quality gate failure alone must be TERMINAL — not SUCCEEDED with warnings."""
    stages = [
        make_stage("validate", WorkflowStatus.SUCCEEDED, {"tests_passed": True}),
        make_stage("quality", WorkflowStatus.FAILED_CONTINUE, {"quality_passed": False, "quality_score": 0.60}),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)
    store = MagicMock()

    finalize_status(workflow, store)

    assert workflow.status == WorkflowStatus.TERMINAL
    store.update_status.assert_called_once_with(workflow)
    wfal_calls = [c for c in mock_emit.call_args_list if c[0][0].value == "WFAL"]
    assert any("quality gate failed" in c[0][1] for c in wfal_calls)


@patch("trust5.core.runner.emit")
def test_finalize_status_overrides_to_terminal_on_review_failure(mock_emit):
    """Review failure alone must be TERMINAL — not SUCCEEDED with warnings."""
    stages = [
        make_stage("validate", WorkflowStatus.SUCCEEDED, {"tests_passed": True}),
        make_stage("review", WorkflowStatus.FAILED_CONTINUE, {"review_passed": False, "review_score": 0.4}),
        make_stage("quality", WorkflowStatus.SUCCEEDED, {"quality_passed": True, "quality_score": 0.90}),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)
    store = MagicMock()

    finalize_status(workflow, store)

    assert workflow.status == WorkflowStatus.TERMINAL
    store.update_status.assert_called_once_with(workflow)
    wfal_calls = [c for c in mock_emit.call_args_list if c[0][0].value == "WFAL"]
    assert any("code review failed" in c[0][1] for c in wfal_calls)


@patch("trust5.core.runner.emit")
def test_finalize_status_clean_succeeded(mock_emit):
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
    workflow = make_workflow(WorkflowStatus.FAILED_CONTINUE, [])
    store = MagicMock()

    finalize_status(workflow, store)

    wfal_calls = [c for c in mock_emit.call_args_list if c[0][0].value == "WFAL"]
    assert len(wfal_calls) >= 1
    assert "incomplete" in wfal_calls[0][0][1].lower()


@patch("trust5.core.runner.emit")
def test_finalize_status_terminal_workflow(mock_emit):
    workflow = make_workflow(WorkflowStatus.TERMINAL, [])
    store = MagicMock()

    finalize_status(workflow, store)

    wfal_calls = [c for c in mock_emit.call_args_list if c[0][0].value == "WFAL"]
    assert len(wfal_calls) >= 1
    assert "TERMINAL" in wfal_calls[0][0][1]


@patch("trust5.core.runner.emit")
def test_finalize_status_both_test_and_quality_failures(mock_emit):
    stages = [
        make_stage("validate", WorkflowStatus.FAILED_CONTINUE, {"tests_passed": False}),
        make_stage("quality", WorkflowStatus.FAILED_CONTINUE, {"quality_passed": False, "quality_score": 0.4}),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)
    store = MagicMock()

    finalize_status(workflow, store)

    assert workflow.status == WorkflowStatus.TERMINAL
    store.update_status.assert_called_once()
    wfal_calls = [c for c in mock_emit.call_args_list if c[0][0].value == "WFAL"]
    failure_msg = wfal_calls[0][0][1]
    assert "tests failing" in failure_msg
    assert "quality" in failure_msg


@patch("trust5.core.runner.emit")
def test_finalize_status_compliance_failure_overrides_to_terminal(mock_emit):
    """compliance_passed=False in outputs triggers TERMINAL."""
    stages = [
        make_stage("validate", WorkflowStatus.SUCCEEDED, {"tests_passed": True}),
        make_stage(
            "quality",
            WorkflowStatus.FAILED_CONTINUE,
            {
                "quality_passed": False,
                "quality_score": 0.975,
                "spec_compliance_ratio": 0.667,
                "compliance_passed": False,
                "spec_criteria_met": 10,
                "spec_criteria_total": 15,
                "spec_unmet_criteria": ["[UBIQ] Missing JSON content-type"],
            },
        ),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)
    store = MagicMock()

    finalize_status(workflow, store)

    assert workflow.status == WorkflowStatus.TERMINAL
    store.update_status.assert_called_once_with(workflow)
    wfal_calls = [c for c in mock_emit.call_args_list if c[0][0].value == "WFAL"]
    assert any("SPEC compliance" in c[0][1] for c in wfal_calls)


@patch("trust5.core.runner.emit")
def test_finalize_status_compliance_071_with_compliance_passed_true(mock_emit):
    """Ratio 0.71 with compliance_passed=True (config threshold <= 0.71) is not a failure."""
    stages = [
        make_stage(
            "quality",
            WorkflowStatus.SUCCEEDED,
            {
                "quality_passed": True,
                "spec_compliance_ratio": 0.71,
                "compliance_passed": True,
                "spec_criteria_met": 12,
                "spec_criteria_total": 17,
            },
        ),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)
    store = MagicMock()

    finalize_status(workflow, store)

    assert workflow.status == WorkflowStatus.SUCCEEDED
    store.update_status.assert_not_called()


@patch("trust5.core.runner.emit")
def test_finalize_status_exact_user_scenario(mock_emit):
    """Reproduces the exact failure: ratio 0.71, quality 0.934, review failed, gate failed.

    This is the bug that was missed in certification. Pipeline must report FAILED, not SUCCEEDED.
    """
    stages = [
        make_stage("validate", WorkflowStatus.SUCCEEDED, {"tests_passed": True}),
        make_stage(
            "review",
            WorkflowStatus.FAILED_CONTINUE,
            {"review_passed": False, "review_score": 0.6},
        ),
        make_stage(
            "quality",
            WorkflowStatus.FAILED_CONTINUE,
            {
                "quality_passed": False,
                "quality_score": 0.934,
                "spec_compliance_ratio": 0.71,
                "compliance_passed": True,
                "spec_criteria_met": 12,
                "spec_criteria_total": 17,
                "spec_unmet_criteria": [
                    "[UBIQ] The API shall return JSON responses",
                    "[EVENT] GET /api/todos/<id>",
                    "[EVENT] PUT /api/todos/<id>",
                    "[EVENT] PATCH /api/todos/<id>",
                    "[EVENT] DELETE /api/todos/<id>",
                ],
            },
        ),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)
    store = MagicMock()

    finalize_status(workflow, store)

    assert workflow.status == WorkflowStatus.TERMINAL
    store.update_status.assert_called_once_with(workflow)
    wfal_calls = [c for c in mock_emit.call_args_list if c[0][0].value == "WFAL"]
    failure_msg = wfal_calls[0][0][1]
    assert "FAILED" in failure_msg
    assert "quality gate failed" in failure_msg
    assert "code review failed" in failure_msg


@patch("trust5.core.runner.emit")
def test_finalize_status_quality_failure_suggests_loop(mock_emit):
    """Quality-only failure should suggest 'trust5 loop', not 'trust5 resume'."""
    stages = [
        make_stage("quality", WorkflowStatus.FAILED_CONTINUE, {"quality_passed": False, "quality_score": 0.60}),
    ]
    workflow = make_workflow(WorkflowStatus.SUCCEEDED, stages)
    store = MagicMock()

    finalize_status(workflow, store)

    wfal_calls = [c for c in mock_emit.call_args_list if c[0][0].value == "WFAL"]
    assert any("trust5 loop" in c[0][1] for c in wfal_calls)
    assert not any("trust5 resume" in c[0][1] for c in wfal_calls)


# ── wait_for_completion tests ──


def test_wait_for_completion_returns_on_terminal():
    store = MagicMock()
    wf = MagicMock()
    wf.status = WorkflowStatus.SUCCEEDED
    store.retrieve.return_value = wf

    result = wait_for_completion(store, "wf-123", timeout=10.0)

    assert result.status == WorkflowStatus.SUCCEEDED


def test_wait_for_completion_returns_early_on_stop_event():
    store = MagicMock()
    wf = MagicMock()
    wf.status = WorkflowStatus.RUNNING
    store.retrieve.return_value = wf

    stop = threading.Event()
    stop.set()

    result = wait_for_completion(store, "wf-123", timeout=600.0, stop_event=stop)

    assert result.status == WorkflowStatus.RUNNING
    assert store.retrieve.call_count == 1


def test_wait_for_completion_ignores_none_stop_event():
    store = MagicMock()
    wf = MagicMock()
    wf.status = WorkflowStatus.SUCCEEDED
    store.retrieve.return_value = wf

    result = wait_for_completion(store, "wf-123", timeout=10.0, stop_event=None)

    assert result.status == WorkflowStatus.SUCCEEDED


# ── Progressive polling tests ──────────────────────────────────────


def test_progressive_poll_constants_exist():
    from trust5.core.runner import POLL_INTERVAL, POLL_INTERVAL_FAST, POLL_INTERVAL_MODERATE, POLL_INTERVAL_SLOW

    assert POLL_INTERVAL_FAST == 0.5
    assert POLL_INTERVAL_MODERATE == 2.0
    assert POLL_INTERVAL_SLOW == 5.0
    assert POLL_INTERVAL == POLL_INTERVAL_FAST


def test_progressive_poll_constants_ordered():
    from trust5.core.runner import POLL_INTERVAL_FAST, POLL_INTERVAL_MODERATE, POLL_INTERVAL_SLOW

    assert POLL_INTERVAL_FAST < POLL_INTERVAL_MODERATE < POLL_INTERVAL_SLOW


@patch("trust5.core.runner.time")
def test_wait_for_completion_uses_fast_poll_initially(mock_time):
    store = MagicMock()

    wf_running = MagicMock()
    wf_running.status = WorkflowStatus.RUNNING
    wf_done = MagicMock()
    wf_done.status = WorkflowStatus.SUCCEEDED

    store.retrieve.side_effect = [wf_running, wf_done]

    mock_time.monotonic.side_effect = [100.0, 100.0, 100.5, 100.0, 100.5, 101.0]
    mock_time.sleep = MagicMock()

    from trust5.core.runner import POLL_INTERVAL_FAST

    result = wait_for_completion(store, "wf-test", timeout=600.0)

    assert result.status == WorkflowStatus.SUCCEEDED
    if mock_time.sleep.called:
        mock_time.sleep.assert_called_with(POLL_INTERVAL_FAST)
