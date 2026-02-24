"""Tests for LLM-based SPEC compliance checker and stagnant bypass."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from trust5.core.compliance import (
    _build_compliance_prompt,
    _check_compliance_keywords,
    _check_compliance_llm,
    _parse_llm_response,
    check_compliance,
)


class TestBuildCompliancePrompt:
    def test_includes_all_criteria(self) -> None:
        criteria = ["[UBIQ] API shall return JSON.", "[EVENT] GET /todos shall list items."]
        prompt = _build_compliance_prompt(criteria, "def app(): pass")
        assert "0. [UBIQ] API shall return JSON." in prompt
        assert "1. [EVENT] GET /todos shall list items." in prompt

    def test_includes_source_code(self) -> None:
        prompt = _build_compliance_prompt(["test criterion"], "class Todo:\n    pass")
        assert "class Todo:" in prompt

    def test_truncates_large_source(self) -> None:
        large_source = "x" * 100_000
        prompt = _build_compliance_prompt(["test"], large_source)
        assert "[... truncated ...]" in prompt
        assert len(prompt) < 100_000


class TestParseLLMResponse:
    def test_parses_code_fenced_json(self) -> None:
        criteria = ["criterion A", "criterion B"]
        raw = (
            "Here is my assessment:\n"
            "```json\n"
            '{"criteria": [{"index": 0, "status": "met", "evidence": "found it"}, '
            '{"index": 1, "status": "not_met", "evidence": "missing"}]}\n'
            "```\n"
        )
        results = _parse_llm_response(raw, criteria)
        assert len(results) == 2
        assert results[0].status == "met"
        assert results[0].criterion == "criterion A"
        assert results[1].status == "not_met"

    def test_parses_bare_json(self) -> None:
        criteria = ["criterion A"]
        raw = '{"criteria": [{"index": 0, "status": "met", "evidence": "ok"}]}'
        results = _parse_llm_response(raw, criteria)
        assert len(results) == 1
        assert results[0].status == "met"

    def test_handles_invalid_json(self) -> None:
        results = _parse_llm_response("not json at all", ["criterion"])
        assert results == []

    def test_handles_missing_criteria_key(self) -> None:
        results = _parse_llm_response('{"foo": "bar"}', ["criterion"])
        assert results == []

    def test_skips_invalid_index(self) -> None:
        criteria = ["only one"]
        raw = '{"criteria": [{"index": 5, "status": "met", "evidence": "wrong index"}]}'
        results = _parse_llm_response(raw, criteria)
        assert len(results) == 0

    def test_normalizes_unknown_status(self) -> None:
        criteria = ["criterion"]
        raw = '{"criteria": [{"index": 0, "status": "unknown", "evidence": "hmm"}]}'
        results = _parse_llm_response(raw, criteria)
        assert results[0].status == "not_met"

    def test_handles_partial_status(self) -> None:
        criteria = ["criterion"]
        raw = '{"criteria": [{"index": 0, "status": "partial", "evidence": "partly done"}]}'
        results = _parse_llm_response(raw, criteria)
        assert results[0].status == "partial"


class TestCheckComplianceLLM:
    def test_returns_report_on_success(self) -> None:
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {
            "message": {
                "content": json.dumps(
                    {
                        "criteria": [
                            {"index": 0, "status": "met", "evidence": "found Todo model"},
                            {"index": 1, "status": "met", "evidence": "found CRUD routes"},
                        ]
                    }
                )
            }
        }
        criteria = ["Todo model exists", "CRUD routes exist"]
        with patch("trust5.core.message.emit"):
            report = _check_compliance_llm(criteria, "class Todo: pass", mock_llm)
        assert report is not None
        assert report.criteria_met == 2
        assert report.compliance_ratio == 1.0

    def test_returns_none_on_llm_error(self) -> None:
        mock_llm = MagicMock()
        mock_llm.chat.side_effect = RuntimeError("LLM down")
        with patch("trust5.core.message.emit"):
            report = _check_compliance_llm(["criterion"], "source", mock_llm)
        assert report is None

    def test_returns_none_on_empty_response(self) -> None:
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {"message": {"content": ""}}
        with patch("trust5.core.message.emit"):
            report = _check_compliance_llm(["criterion"], "source", mock_llm)
        assert report is None

    def test_returns_none_on_unparseable_response(self) -> None:
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {"message": {"content": "I can't help with that."}}
        with patch("trust5.core.message.emit"):
            report = _check_compliance_llm(["criterion"], "source", mock_llm)
        assert report is None

    def test_fills_missing_criteria(self) -> None:
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {
            "message": {"content": json.dumps({"criteria": [{"index": 0, "status": "met", "evidence": "ok"}]})}
        }
        criteria = ["criterion A", "criterion B"]
        with patch("trust5.core.message.emit"):
            report = _check_compliance_llm(criteria, "source", mock_llm)
        assert report is not None
        assert report.criteria_total == 2
        assert report.criteria_met == 1
        assert report.criteria_not_met == 1


class TestCheckComplianceDispatch:
    def test_uses_llm_when_provided(self, tmp_path: Path) -> None:
        src = tmp_path / "app.py"
        src.write_text("def hello(): pass\n")
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {
            "message": {"content": json.dumps({"criteria": [{"index": 0, "status": "met", "evidence": "found it"}]})}
        }
        criteria = ["[UBIQ] The system shall say hello."]
        with patch("trust5.core.message.emit"):
            report = check_compliance(criteria, str(tmp_path), extensions=(".py",), llm=mock_llm)
        assert report.criteria_met == 1
        mock_llm.chat.assert_called_once()

    def test_falls_back_to_keywords_on_llm_failure(self, tmp_path: Path) -> None:
        src = tmp_path / "sim.py"
        src.write_text("class MonteCarloSimulator:\n    pass\n")
        mock_llm = MagicMock()
        mock_llm.chat.side_effect = RuntimeError("LLM unavailable")
        criteria = ["[UBIQ] The MonteCarloSimulator shall work."]
        with patch("trust5.core.message.emit"):
            report = check_compliance(criteria, str(tmp_path), extensions=(".py",), llm=mock_llm)
        assert report.criteria_met == 1
        assert report.results[0].searched_identifiers != ("llm-assessed",)

    def test_uses_keywords_when_no_llm(self, tmp_path: Path) -> None:
        src = tmp_path / "sim.py"
        src.write_text("class MonteCarloSimulator:\n    pass\n")
        criteria = ["[UBIQ] The MonteCarloSimulator shall work."]
        report = check_compliance(criteria, str(tmp_path), extensions=(".py",))
        assert report.criteria_met == 1

    def test_empty_source_returns_all_unmet(self, tmp_path: Path) -> None:
        criteria = ["[UBIQ] Something should exist."]
        report = check_compliance(criteria, str(tmp_path), extensions=(".py",))
        assert report.criteria_met == 0
        assert report.criteria_not_met == 1
        assert report.compliance_ratio == 0.0


class TestKeywordFallback:
    def test_keyword_compliance_basic(self) -> None:
        source = "class MonteCarloSimulator:\n    pass\n"
        criteria = ["[UBIQ] The MonteCarloSimulator shall work."]
        report = _check_compliance_keywords(criteria, source)
        assert report.criteria_met == 1

    def test_keyword_compliance_no_match(self) -> None:
        source = "def foo(): pass\n"
        criteria = ["[UBIQ] The MonteCarloSimulator shall work."]
        report = _check_compliance_keywords(criteria, source)
        assert report.criteria_met == 0

    def test_keyword_compliance_empty_identifiers(self) -> None:
        source = "def foo(): pass\n"
        criteria = ["[UBIQ] The system shall work correctly."]
        report = _check_compliance_keywords(criteria, source)
        assert report.criteria_met == 1


class TestStagnantBypassForCompliance:
    @pytest.fixture()
    def _mock_deps(self):
        with (
            patch("trust5.tasks.quality_task.TrustGate") as mock_gate_cls,
            patch("trust5.tasks.quality_task.emit"),
            patch("trust5.tasks.quality_task.emit_block"),
            patch("trust5.tasks.quality_task.signal_pipeline_done"),
            patch("trust5.tasks.quality_task.ConfigManager"),
            patch("trust5.tasks.quality_task.build_snapshot_from_report"),
            patch("trust5.tasks.quality_task.validate_phase", return_value=[]),
            patch("trust5.tasks.quality_task.validate_methodology", return_value=[]),
        ):
            mock_report = MagicMock()
            mock_report.score = 0.975
            mock_report.passed = True
            mock_report.total_errors = 0
            mock_report.total_warnings = 0
            mock_report.coverage_pct = 95
            mock_report.principles = {}
            mock_report.model_dump.return_value = {
                "score": 0.975,
                "total_errors": 0,
                "total_warnings": 0,
            }
            mock_gate_cls.return_value.validate.return_value = mock_report
            yield mock_report

    def test_stagnant_bypassed_when_compliance_is_only_blocker(self, _mock_deps: MagicMock) -> None:
        from trust5.tasks.quality_task import QualityTask

        task = QualityTask()
        stage = MagicMock()
        stage.context = {
            "project_root": "/tmp/test",
            "quality_attempt": 1,
            "max_quality_attempts": 3,
            "language_profile": {},
            "prev_quality_report": {
                "score": 0.975,
                "total_errors": 0,
                "total_warnings": 0,
            },
            "plan_config": {"acceptance_criteria": []},
        }

        with patch("trust5.tasks.quality_task.is_stagnant", return_value=True):
            with patch("trust5.tasks.quality_task.meets_quality_gate", return_value=True):
                result = task.execute(stage)

        assert result.status.name == "SUCCEEDED"
