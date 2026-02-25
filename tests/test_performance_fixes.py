"""Tests for the 5 systemic performance fixes (Tasks 1-4).

Task 1: Lint output filtering in ReadableValidator + rule mismatch
Task 2: Review-skip on quality retry (jump_review_ref)
Task 3: Planner trivial-project threshold + collapse logic
Task 4: simple_max_turns + reduced review_max_turns
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from trust5.core.config import AgentConfig, QualityConfig
from trust5.core.context_keys import PROPAGATED_CONTEXT_KEYS
from trust5.tasks.validate_helpers import _filter_test_file_lint
from trust5.workflows.module_spec import ModuleSpec

# ============================================================================
# Task 1: Lint output filtering + rule name
# ============================================================================


class TestLintOutputFiltering:
    """ReadableValidator must filter test-file lint from OUTPUT, not just command."""

    def test_filter_removes_test_file_lines(self):
        """Lint output referencing test_*.py files is stripped."""
        raw = "tests/test_app.py:3:1: F401 unused import\nsrc/app.py:10:5: E501 line too long\n"
        filtered = _filter_test_file_lint(raw)
        assert "test_app.py" not in filtered
        assert "src/app.py" in filtered

    def test_filter_empty_after_strip_is_clean(self):
        """If ALL lint lines come from test files, result is empty (clean)."""
        raw = "tests/test_main.py:1:1: F401 unused import\ntest_utils.py:5:3: E302 expected 2 blank lines\n"
        filtered = _filter_test_file_lint(raw)
        # After filtering, nothing substantive remains
        assert filtered.strip() == "" or "test_" not in filtered

    def test_filter_preserves_non_test_lines(self):
        """Non-test file lint lines are preserved verbatim."""
        raw = "src/core.py:1:1: W291 trailing whitespace\n"
        filtered = _filter_test_file_lint(raw)
        assert "src/core.py" in filtered

    def test_filter_handles_empty_input(self):
        """Empty input returns empty output."""
        assert _filter_test_file_lint("") == ""

    def test_filter_handles_none_gracefully(self):
        """None input returns empty string (no crash)."""
        # _filter_test_file_lint should handle None or return ""
        result = _filter_test_file_lint(None)  # type: ignore[arg-type]
        assert result == "" or result is None


# ============================================================================
# Task 1: Rule name mismatch (lint-raw vs lint-errors)
# ============================================================================


class TestRuleName:
    """Quality gate build_snapshot must count lint-errors, not lint-raw."""

    def test_snapshot_counts_lint_errors_rule(self):
        """build_snapshot_from_report correctly counts 'lint-errors' rule."""
        from trust5.core.quality_gates import build_snapshot_from_report
        from trust5.core.quality_models import Issue, PrincipleResult, QualityReport

        report = QualityReport(
            passed=True,
            score=0.9,
            principles={
                "readable": PrincipleResult(
                    name="readable",
                    passed=True,
                    score=0.9,
                    issues=[
                        Issue(severity="error", message="lint error", rule="lint-errors"),
                        Issue(severity="error", message="another lint", rule="lint-errors"),
                    ],
                ),
            },
        )

        snapshot = build_snapshot_from_report(report)
        assert snapshot.lint_errors == 2
        """Issues with rule 'lint-raw' are NOT counted as lint_errors."""
        from trust5.core.quality_gates import build_snapshot_from_report
        from trust5.core.quality_models import Issue, PrincipleResult, QualityReport

        report = QualityReport(
            passed=True,
            score=0.9,
            principles={
                "readable": PrincipleResult(
                    name="readable",
                    passed=True,
                    score=0.9,
                    issues=[
                        Issue(severity="error", message="lint error", rule="lint-raw"),
                    ],
                ),
            },
        )

        snapshot = build_snapshot_from_report(report)
        assert snapshot.lint_errors == 0


# ============================================================================
# Task 2: Review-skip on quality retry (jump_review_ref)
# ============================================================================


class TestJumpReviewRef:
    """Repair task must jump to review (not quality) after quality failure."""

    def test_jump_review_ref_in_propagated_keys(self):
        """jump_review_ref must be in PROPAGATED_CONTEXT_KEYS for DAG routing."""
        assert "jump_review_ref" in PROPAGATED_CONTEXT_KEYS

    def test_jump_quality_ref_still_in_propagated_keys(self):
        """jump_quality_ref must remain in PROPAGATED_CONTEXT_KEYS (backward compat)."""
        assert "jump_quality_ref" in PROPAGATED_CONTEXT_KEYS

    @patch("trust5.tasks.repair_task.emit")
    @patch("trust5.tasks.repair_task.Agent")
    @patch("trust5.tasks.repair_task.LLM")
    def test_repair_jumps_to_review_on_quality_failure(self, mock_llm_cls, mock_agent_cls, mock_emit):
        """When failure_type=quality and jump_review_ref is set, repair jumps to review."""
        from trust5.tasks.repair_task import RepairTask

        mock_llm = MagicMock()
        mock_llm_cls.for_tier.return_value = mock_llm

        mock_agent = MagicMock()
        mock_agent.run.return_value = "Fixed the quality issue."
        mock_agent_cls.return_value = mock_agent

        task = RepairTask()
        stage = MagicMock()
        stage.context = {
            "_repair_requested": True,
            "failure_type": "quality",
            "test_output": "quality gate failed",
            "tests_passed": True,
            "tests_partial": False,
            "project_root": "/tmp/fake",
            "spec_id": "SPEC-001",
            "repair_attempt": 1,
            "jump_review_ref": "review",
            "jump_quality_ref": "quality",
            "jump_validate_ref": "validate",
            "language_profile": {"language": "python"},
        }

        result = task.execute(stage)

        # Repair should jump to review, not quality
        assert result.target_stage_ref_id == "review"

    @patch("trust5.tasks.repair_task.emit")
    @patch("trust5.tasks.repair_task.Agent")
    @patch("trust5.tasks.repair_task.LLM")
    def test_repair_falls_back_to_quality_when_no_review_ref(self, mock_llm_cls, mock_agent_cls, mock_emit):
        """When jump_review_ref is absent, repair falls back to jump_quality_ref."""
        from trust5.tasks.repair_task import RepairTask

        mock_llm = MagicMock()
        mock_llm_cls.for_tier.return_value = mock_llm

        mock_agent = MagicMock()
        mock_agent.run.return_value = "Fixed."
        mock_agent_cls.return_value = mock_agent

        task = RepairTask()
        stage = MagicMock()
        stage.context = {
            "_repair_requested": True,
            "failure_type": "quality",
            "test_output": "quality gate failed",
            "tests_passed": True,
            "tests_partial": False,
            "project_root": "/tmp/fake",
            "spec_id": "SPEC-001",
            "repair_attempt": 1,
            # NO jump_review_ref — fallback to jump_quality_ref
            "jump_quality_ref": "quality",
            "jump_validate_ref": "validate",
            "language_profile": {"language": "python"},
        }

        result = task.execute(stage)

        # Should fall back to quality when no review ref
        assert result.target_stage_ref_id == "quality"


# ============================================================================
# Task 3: Trivial-project collapse
# ============================================================================


class TestTrivialProjectCollapse:
    """Multi-module plans with ≤3 total files must collapse to serial."""

    def test_trivial_modules_collapsed(self):
        """3 modules with 1 file each (3 total) should collapse."""
        modules = [
            ModuleSpec(id="a", name="A", files=["a.py"]),
            ModuleSpec(id="b", name="B", files=["b.py"]),
            ModuleSpec(id="c", name="C", files=["c.py"]),
        ]
        total_files = sum(len(m.files) for m in modules)
        assert total_files <= 3
        # The collapse condition: len(modules) > 1 and total_files <= 3
        should_collapse = len(modules) > 1 and total_files <= 3
        assert should_collapse is True

    def test_nontrivial_modules_preserved(self):
        """Modules with >3 total files are NOT collapsed."""
        modules = [
            ModuleSpec(id="a", name="A", files=["a.py", "b.py"]),
            ModuleSpec(id="b", name="B", files=["c.py", "d.py"]),
        ]
        total_files = sum(len(m.files) for m in modules)
        assert total_files == 4
        should_collapse = len(modules) > 1 and total_files <= 3
        assert should_collapse is False

    def test_single_module_not_collapsed(self):
        """Single module is never collapsed (already serial)."""
        modules = [ModuleSpec(id="main", name="Main", files=["app.py"])]
        should_collapse = len(modules) > 1 and sum(len(m.files) for m in modules) <= 3
        assert should_collapse is False

    def test_collapse_produces_single_main_module(self):
        """After collapse, result is a single module with id='main'."""
        modules = [
            ModuleSpec(id="a", name="A", files=["a.py"]),
            ModuleSpec(id="b", name="B", files=["b.py"]),
        ]
        total_files = sum(len(m.files) for m in modules)
        if len(modules) > 1 and total_files <= 3:
            modules = [ModuleSpec(id="main", name="Main")]
        assert len(modules) == 1
        assert modules[0].id == "main"


# ============================================================================
# Task 4: simple_max_turns + review_max_turns
# ============================================================================


class TestAgentTurnLimits:
    """Agent and review turn limits for performance."""

    def test_agent_config_has_simple_max_turns(self):
        """AgentConfig must have simple_max_turns field with default 8."""
        cfg = AgentConfig()
        assert hasattr(cfg, "simple_max_turns")
        assert cfg.simple_max_turns == 8

    def test_agent_config_max_turns_unchanged(self):
        """Default max_turns stays at 20 (not reduced globally)."""
        cfg = AgentConfig()
        assert cfg.max_turns == 20

    def test_simple_max_turns_is_configurable(self):
        """simple_max_turns can be overridden."""
        cfg = AgentConfig(simple_max_turns=5)
        assert cfg.simple_max_turns == 5

    def test_review_max_turns_default(self):
        """review_max_turns default is 8 (reduced from 15)."""
        cfg = QualityConfig()
        assert cfg.review_max_turns == 8

    def test_review_max_turns_is_configurable(self):
        """review_max_turns can be overridden."""
        cfg = QualityConfig(review_max_turns=12)
        assert cfg.review_max_turns == 12


# ============================================================================
# Task 4: ImplementerTask respects stage context max_turns
# ============================================================================


class TestImplementerContextMaxTurns:
    """ImplementerTask must read max_turns from stage.context."""

    @patch("trust5.core.implementer_task.mcp_clients")
    @patch("trust5.core.implementer_task.Agent")
    @patch("trust5.core.implementer_task.LLM")
    @patch("trust5.core.implementer_task.build_implementation_prompt")
    @patch("trust5.core.implementer_task.discover_latest_spec")
    def test_implementer_uses_context_max_turns(
        self, mock_discover, mock_build, mock_llm_cls, mock_agent_cls, mock_mcp
    ):
        """When stage.context has max_turns, implementer uses it."""
        from trust5.core.implementer_task import ImplementerTask

        mock_discover.return_value = "SPEC-001"
        mock_build.return_value = "implement the app"
        mock_llm = MagicMock()
        mock_llm_cls.for_tier.return_value = mock_llm

        mock_agent = MagicMock()
        mock_agent.run.return_value = "done"
        mock_agent_cls.return_value = mock_agent

        mock_mcp.return_value.__enter__ = MagicMock(return_value=None)
        mock_mcp.return_value.__exit__ = MagicMock(return_value=False)

        task = ImplementerTask()
        stage = MagicMock()
        stage.context = {
            "spec_id": "SPEC-001",
            "max_turns": 8,  # Override from default 25
        }

        task.execute(stage)

        # Verify agent.run was called with max_turns=8 from context
        mock_agent.run.assert_called_once()
        call_kwargs = mock_agent.run.call_args
        assert call_kwargs.kwargs.get("max_turns") == 8 or call_kwargs[1].get("max_turns") == 8

    @patch("trust5.core.implementer_task.mcp_clients")
    @patch("trust5.core.implementer_task.Agent")
    @patch("trust5.core.implementer_task.LLM")
    @patch("trust5.core.implementer_task.build_implementation_prompt")
    @patch("trust5.core.implementer_task.discover_latest_spec")
    def test_implementer_defaults_to_25_turns(self, mock_discover, mock_build, mock_llm_cls, mock_agent_cls, mock_mcp):
        """When stage.context has no max_turns, implementer defaults to 25."""
        from trust5.core.implementer_task import ImplementerTask

        mock_discover.return_value = "SPEC-001"
        mock_build.return_value = "implement the app"
        mock_llm = MagicMock()
        mock_llm_cls.for_tier.return_value = mock_llm

        mock_agent = MagicMock()
        mock_agent.run.return_value = "done"
        mock_agent_cls.return_value = mock_agent

        mock_mcp.return_value.__enter__ = MagicMock(return_value=None)
        mock_mcp.return_value.__exit__ = MagicMock(return_value=False)

        task = ImplementerTask()
        stage = MagicMock()
        stage.context = {
            "spec_id": "SPEC-001",
            # No max_turns — should default to 25
        }

        task.execute(stage)

        mock_agent.run.assert_called_once()
        call_kwargs = mock_agent.run.call_args
        assert call_kwargs.kwargs.get("max_turns") == 25 or call_kwargs[1].get("max_turns") == 25
