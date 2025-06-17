from trust5.core.plan_parser import PlanConfig, _parse_acceptance_criteria, parse_plan_output


class TestParseSetupCommands:
    def test_extracts_setup_commands(self) -> None:
        raw = """
Some plan text...

SETUP_COMMANDS:
- python3 -m venv .venv
- .venv/bin/pip install flask pytest

More text...
"""
        config = parse_plan_output(raw)
        assert config.setup_commands == (
            "python3 -m venv .venv",
            ".venv/bin/pip install flask pytest",
        )

    def test_no_setup_commands_returns_empty(self) -> None:
        config = parse_plan_output("Just a plan with no setup block.")
        assert config.setup_commands == ()

    def test_empty_setup_block(self) -> None:
        raw = """
SETUP_COMMANDS:

QUALITY_CONFIG:
  quality_threshold: 0.80
"""
        config = parse_plan_output(raw)
        assert config.setup_commands == ()


class TestParseQualityConfig:
    def test_extracts_quality_threshold(self) -> None:
        raw = """
QUALITY_CONFIG:
  quality_threshold: 0.90
  test_command: .venv/bin/python -m pytest -v
  lint_command: .venv/bin/python -m ruff check .
"""
        config = parse_plan_output(raw)
        assert config.quality_threshold == 0.90
        assert config.test_command == ".venv/bin/python -m pytest -v"
        assert config.lint_command == ".venv/bin/python -m ruff check ."

    def test_clamps_threshold_to_range(self) -> None:
        raw = """
QUALITY_CONFIG:
  quality_threshold: 0.99
"""
        config = parse_plan_output(raw)
        assert config.quality_threshold == 0.95

        raw_low = """
QUALITY_CONFIG:
  quality_threshold: 0.50
"""
        config_low = parse_plan_output(raw_low)
        assert config_low.quality_threshold == 0.70

    def test_no_quality_config_returns_defaults(self) -> None:
        config = parse_plan_output("Just a plan.")
        assert config.quality_threshold == 0.85
        assert config.test_command is None
        assert config.lint_command is None

    def test_coverage_command_extracted(self) -> None:
        raw = """
QUALITY_CONFIG:
  quality_threshold: 0.85
  coverage_command: .venv/bin/python -m pytest --cov=. -q
"""
        config = parse_plan_output(raw)
        assert config.coverage_command == ".venv/bin/python -m pytest --cov=. -q"


class TestPlanConfigToDict:
    def test_round_trips(self) -> None:
        config = PlanConfig(
            setup_commands=("cmd1", "cmd2"),
            quality_threshold=0.88,
            test_command="pytest -v",
            lint_command="ruff check .",
            coverage_command=None,
        )
        d = config.to_dict()
        assert d["setup_commands"] == ["cmd1", "cmd2"]
        assert d["quality_threshold"] == 0.88
        assert d["test_command"] == "pytest -v"
        assert d["lint_command"] == "ruff check ."
        assert d["coverage_command"] is None


class TestParseAcceptanceCriteria:
    def test_extracts_ears_tagged_lines(self) -> None:
        raw = """
## Acceptance Criteria (EARS Format)

- [UBIQ] The system shall return JSON responses.
- [EVENT] When a user submits invalid input, the system shall return a 422 error.
- [STATE] While the database is read-only, the system shall queue writes.
- [UNWNT] If the auth token expires, then the system shall return 401.
- [OPTNL] Where caching is enabled, the system shall cache responses.
- [COMPLX] While in maintenance mode, when a request arrives, the system shall return 503.
"""
        criteria = _parse_acceptance_criteria(raw)
        assert len(criteria) == 6
        assert criteria[0] == "[UBIQ] The system shall return JSON responses."
        assert criteria[1] == "[EVENT] When a user submits invalid input, the system shall return a 422 error."
        assert criteria[5] == "[COMPLX] While in maintenance mode, when a request arrives, the system shall return 503."

    def test_returns_empty_when_no_criteria(self) -> None:
        raw = "Just a plan with no EARS tags at all."
        criteria = _parse_acceptance_criteria(raw)
        assert criteria == []

    def test_works_with_mixed_content(self) -> None:
        raw = """
SETUP_COMMANDS:
- pip install flask

## Acceptance Criteria
- [UBIQ] The API shall use UTF-8 encoding.
- This line is not tagged and should be ignored.
- [EVENT] When health check is called, the system shall return 200.

QUALITY_CONFIG:
  quality_threshold: 0.85
"""
        config = parse_plan_output(raw)
        assert len(config.acceptance_criteria) == 2
        assert config.acceptance_criteria[0] == "[UBIQ] The API shall use UTF-8 encoding."
        assert config.acceptance_criteria[1] == "[EVENT] When health check is called, the system shall return 200."

    def test_case_insensitive_tags(self) -> None:
        raw = "- [ubiq] The system shall do something."
        criteria = _parse_acceptance_criteria(raw)
        assert len(criteria) == 1
        assert criteria[0] == "[UBIQ] The system shall do something."

    def test_to_dict_includes_criteria(self) -> None:
        config = PlanConfig(
            acceptance_criteria=("[UBIQ] Criterion one.", "[EVENT] Criterion two."),
        )
        d = config.to_dict()
        assert d["acceptance_criteria"] == ["[UBIQ] Criterion one.", "[EVENT] Criterion two."]

    def test_default_acceptance_criteria_empty(self) -> None:
        config = PlanConfig()
        assert config.acceptance_criteria == ()
        assert config.to_dict()["acceptance_criteria"] == []


class TestFullPlanOutput:
    def test_realistic_plan(self) -> None:
        raw = """
PROJECT NAME: hello-flask
DESCRIPTION: A simple Flask web application

## File Structure
- app.py  # Flask application
- tests/test_app.py  # Tests

## Dependencies
- flask  # Web framework
- pytest  # Testing

SETUP_COMMANDS:
- python3 -m venv .venv
- .venv/bin/pip install flask pytest pytest-cov ruff

QUALITY_CONFIG:
  quality_threshold: 0.85
  test_command: .venv/bin/python -m pytest -v --tb=long -x
  lint_command: .venv/bin/python -m ruff check .
  coverage_command: .venv/bin/python -m pytest --cov=. --cov-report=term-missing -q

## Acceptance Criteria (EARS Format)
- [UBIQ] The API shall return JSON responses.
"""
        config = parse_plan_output(raw)
        assert len(config.setup_commands) == 2
        assert config.quality_threshold == 0.85
        assert ".venv/bin/python -m pytest -v --tb=long -x" == config.test_command
        assert ".venv/bin/python -m ruff check ." == config.lint_command
        assert config.coverage_command is not None
        assert len(config.acceptance_criteria) == 1
        assert config.acceptance_criteria[0] == "[UBIQ] The API shall return JSON responses."
