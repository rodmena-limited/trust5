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
