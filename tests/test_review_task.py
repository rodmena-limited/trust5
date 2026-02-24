"""Tests for trust5/tasks/review_task.py — ReviewTask and parse_review_findings."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from stabilize.models.status import WorkflowStatus

from trust5.core.config import QualityConfig
from trust5.tasks.review_task import ReviewTask, parse_review_findings


def make_stage(context: dict | None = None) -> MagicMock:
    stage = MagicMock()
    stage.context = context or {}
    stage.context.setdefault("project_root", "/tmp/fake-project")
    return stage


# ── parse_review_findings tests ──────────────────────────────────────────


def test_parse_findings_from_llm_output():
    """Valid JSON block is parsed into ReviewReport."""
    findings_json = json.dumps(
        {
            "findings": [
                {
                    "severity": "warning",
                    "category": "code-duplication",
                    "file": "src/core.py",
                    "line": 42,
                    "description": "Duplicate logic in analysis.py",
                },
                {
                    "severity": "error",
                    "category": "deprecated-api",
                    "file": "src/main.py",
                    "line": 10,
                    "description": "np.random.seed() is deprecated",
                },
            ],
            "summary_score": 0.75,
            "total_errors": 1,
            "total_warnings": 1,
            "total_info": 0,
        }
    )
    raw = f"Some review text\n<!-- REVIEW_FINDINGS JSON\n{findings_json}\n-->\nMore text"

    report = parse_review_findings(raw)

    assert len(report.findings) == 2
    assert report.summary_score == 0.75
    assert report.total_errors == 1
    assert report.total_warnings == 1
    assert report.findings[0].category == "code-duplication"
    assert report.findings[1].severity == "error"


def test_parse_findings_fallback_on_malformed_json():
    """Malformed JSON produces a fallback info finding."""
    raw = "<!-- REVIEW_FINDINGS JSON\n{invalid json here}\n-->"

    report = parse_review_findings(raw)

    assert len(report.findings) == 1
    assert report.findings[0].severity == "info"
    assert report.summary_score == 0.85


def test_parse_findings_no_json_block():
    """No JSON block at all produces a fallback info finding."""
    raw = "The code looks fine overall. No issues found."

    report = parse_review_findings(raw)

    assert len(report.findings) == 1
    assert report.findings[0].severity == "info"
    assert report.summary_score == 0.85


def test_parse_findings_empty_findings_array():
    """Empty findings array with perfect score."""
    findings_json = json.dumps(
        {
            "findings": [],
            "summary_score": 1.0,
            "total_errors": 0,
            "total_warnings": 0,
            "total_info": 0,
        }
    )
    raw = f"<!-- REVIEW_FINDINGS JSON\n{findings_json}\n-->"

    report = parse_review_findings(raw)

    assert len(report.findings) == 0
    assert report.summary_score == 1.0
    assert report.total_errors == 0


# ── ReviewTask.execute tests ─────────────────────────────────────────────


@patch("trust5.tasks.review_task.emit")
@patch("trust5.tasks.review_task.emit_block")
@patch("trust5.tasks.review_task.ConfigManager")
def test_review_skipped_when_disabled(mock_config_mgr, mock_emit_block, mock_emit):
    """When code_review_enabled=False, review is skipped."""
    config = QualityConfig(code_review_enabled=False)
    mock_mgr_inst = MagicMock()
    mock_mgr_inst.load_config.return_value = MagicMock(quality=config)
    mock_config_mgr.return_value = mock_mgr_inst

    task = ReviewTask()
    stage = make_stage()

    result = task.execute(stage)

    assert result.status == WorkflowStatus.SUCCEEDED
    assert result.outputs["review_passed"] is True
    assert result.outputs["review_skipped"] is True


@patch("trust5.tasks.review_task.emit")
@patch("trust5.tasks.review_task.emit_block")
@patch("trust5.tasks.review_task.mcp_clients")
@patch("trust5.tasks.review_task.Agent")
@patch("trust5.tasks.review_task.LLM")
@patch("trust5.tasks.review_task.ConfigManager")
def test_review_passes_with_no_findings(
    mock_config_mgr,
    mock_llm_cls,
    mock_agent_cls,
    mock_mcp,
    mock_emit_block,
    mock_emit,
):
    """Review with no error findings and high score passes."""
    config = QualityConfig(code_review_enabled=True)
    mock_mgr_inst = MagicMock()
    mock_mgr_inst.load_config.return_value = MagicMock(quality=config)
    mock_config_mgr.return_value = mock_mgr_inst

    findings_json = json.dumps(
        {
            "findings": [],
            "summary_score": 0.95,
            "total_errors": 0,
            "total_warnings": 0,
            "total_info": 0,
        }
    )
    agent_output = f"Code looks great.\n<!-- REVIEW_FINDINGS JSON\n{findings_json}\n-->"

    mock_agent = MagicMock()
    mock_agent.run.return_value = agent_output
    mock_agent_cls.return_value = mock_agent

    mock_llm_cls.for_tier.return_value = MagicMock()
    mock_mcp.return_value.__enter__ = MagicMock(return_value=[])
    mock_mcp.return_value.__exit__ = MagicMock(return_value=False)

    task = ReviewTask()
    stage = make_stage({"language_profile": {"language": "python", "extensions": [".py"]}})

    result = task.execute(stage)

    assert result.status == WorkflowStatus.SUCCEEDED
    assert result.outputs["review_passed"] is True
    assert result.outputs["review_score"] == 0.95


@patch("trust5.tasks.review_task.emit")
@patch("trust5.tasks.review_task.emit_block")
@patch("trust5.tasks.review_task.mcp_clients")
@patch("trust5.tasks.review_task.Agent")
@patch("trust5.tasks.review_task.LLM")
@patch("trust5.tasks.review_task.ConfigManager")
def test_review_advisory_on_errors(
    mock_config_mgr,
    mock_llm_cls,
    mock_agent_cls,
    mock_mcp,
    mock_emit_block,
    mock_emit,
):
    """Review with errors returns failed_continue (advisory, not terminal)."""
    config = QualityConfig(code_review_enabled=True, code_review_jump_to_repair=False)
    mock_mgr_inst = MagicMock()
    mock_mgr_inst.load_config.return_value = MagicMock(quality=config)
    mock_config_mgr.return_value = mock_mgr_inst

    findings_json = json.dumps(
        {
            "findings": [
                {
                    "severity": "error",
                    "category": "code-duplication",
                    "file": "src/core.py",
                    "line": 42,
                    "description": "Duplicate code",
                }
            ],
            "summary_score": 0.65,
            "total_errors": 1,
            "total_warnings": 0,
            "total_info": 0,
        }
    )
    agent_output = f"Found issues.\n<!-- REVIEW_FINDINGS JSON\n{findings_json}\n-->"

    mock_agent = MagicMock()
    mock_agent.run.return_value = agent_output
    mock_agent_cls.return_value = mock_agent

    mock_llm_cls.for_tier.return_value = MagicMock()
    mock_mcp.return_value.__enter__ = MagicMock(return_value=[])
    mock_mcp.return_value.__exit__ = MagicMock(return_value=False)

    task = ReviewTask()
    stage = make_stage({"language_profile": {"language": "python", "extensions": [".py"]}})

    result = task.execute(stage)

    assert result.status == WorkflowStatus.FAILED_CONTINUE
    assert result.outputs["review_passed"] is False
    assert result.outputs["review_score"] == 0.65
    # Should NOT jump to repair (advisory mode)
    assert result.target_stage_ref_id is None


@patch("trust5.tasks.review_task.emit")
@patch("trust5.tasks.review_task.emit_block")
@patch("trust5.tasks.review_task.mcp_clients")
@patch("trust5.tasks.review_task.Agent")
@patch("trust5.tasks.review_task.LLM")
@patch("trust5.tasks.review_task.ConfigManager")
@patch("trust5.tasks.review_task.propagate_context")
def test_review_jump_to_repair_when_enabled(
    mock_propagate,
    mock_config_mgr,
    mock_llm_cls,
    mock_agent_cls,
    mock_mcp,
    mock_emit_block,
    mock_emit,
):
    """When code_review_jump_to_repair=True and errors found, jumps to repair."""
    config = QualityConfig(code_review_enabled=True, code_review_jump_to_repair=True)
    mock_mgr_inst = MagicMock()
    mock_mgr_inst.load_config.return_value = MagicMock(quality=config)
    mock_config_mgr.return_value = mock_mgr_inst

    findings_json = json.dumps(
        {
            "findings": [
                {
                    "severity": "error",
                    "category": "deprecated-api",
                    "file": "src/main.py",
                    "line": 10,
                    "description": "Deprecated API usage",
                }
            ],
            "summary_score": 0.60,
            "total_errors": 1,
            "total_warnings": 0,
            "total_info": 0,
        }
    )
    agent_output = f"<!-- REVIEW_FINDINGS JSON\n{findings_json}\n-->"

    mock_agent = MagicMock()
    mock_agent.run.return_value = agent_output
    mock_agent_cls.return_value = mock_agent

    mock_llm_cls.for_tier.return_value = MagicMock()
    mock_mcp.return_value.__enter__ = MagicMock(return_value=[])
    mock_mcp.return_value.__exit__ = MagicMock(return_value=False)

    task = ReviewTask()
    stage = make_stage({"language_profile": {"language": "python", "extensions": [".py"]}})

    result = task.execute(stage)

    assert result.status == WorkflowStatus.REDIRECT
    assert result.target_stage_ref_id == "repair"
    assert result.context["failure_type"] == "review"
    assert result.context["_repair_requested"] is True


@patch("trust5.tasks.review_task.emit")
@patch("trust5.tasks.review_task.emit_block")
@patch("trust5.tasks.review_task.mcp_clients")
@patch("trust5.tasks.review_task.Agent")
@patch("trust5.tasks.review_task.LLM")
@patch("trust5.tasks.review_task.ConfigManager")
def test_language_context_injected_into_prompt(
    mock_config_mgr,
    mock_llm_cls,
    mock_agent_cls,
    mock_mcp,
    mock_emit_block,
    mock_emit,
):
    """The review prompt includes language-specific context from build_language_context."""
    config = QualityConfig(code_review_enabled=True)
    mock_mgr_inst = MagicMock()
    mock_mgr_inst.load_config.return_value = MagicMock(quality=config)
    mock_config_mgr.return_value = mock_mgr_inst

    findings_json = json.dumps(
        {
            "findings": [],
            "summary_score": 1.0,
            "total_errors": 0,
            "total_warnings": 0,
            "total_info": 0,
        }
    )
    agent_output = f"<!-- REVIEW_FINDINGS JSON\n{findings_json}\n-->"

    mock_agent = MagicMock()
    mock_agent.run.return_value = agent_output
    mock_agent_cls.return_value = mock_agent

    mock_llm_cls.for_tier.return_value = MagicMock()
    mock_mcp.return_value.__enter__ = MagicMock(return_value=[])
    mock_mcp.return_value.__exit__ = MagicMock(return_value=False)

    task = ReviewTask()
    stage = make_stage(
        {
            "language_profile": {
                "language": "python",
                "extensions": [".py"],
                "test_command": ["pytest"],
                "test_verify_command": 'Bash("pytest")',
                "lint_commands": ["ruff check"],
                "prompt_hints": "Language: Python.",
            }
        }
    )

    task.execute(stage)

    # The agent's run() was called — check that the prompt contains language context
    call_args = mock_agent.run.call_args
    prompt_text = call_args[0][0]  # first positional arg
    assert "Project Language" in prompt_text
    assert "Python" in prompt_text
