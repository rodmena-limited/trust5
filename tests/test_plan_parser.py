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
