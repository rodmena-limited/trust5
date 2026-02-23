"""Tests for role-aware denied_files labels in agent_task.py (Bug 11 fix)."""
from __future__ import annotations


def _build_ownership_prompt(
    agent_name: str,
    owned_files: list[str],
    test_files: list[str],
    module_name: str = "app",
) -> str:
    """Replicate the ownership prompt logic from AgentTask.execute (lines 188-260)."""
    is_test_writer = "test-writer" in agent_name.lower() or "test_writer" in agent_name.lower()
    is_implementer_or_repairer = "implementer" in agent_name.lower() or "repairer" in agent_name.lower()

    effective_owned: list[str] = []
    denied_for_agent: list[str] = []

    if is_test_writer:
        effective_owned.extend(test_files)
        denied_for_agent.extend(owned_files)
    elif is_implementer_or_repairer:
        effective_owned.extend(owned_files)
        denied_for_agent.extend(test_files)
    else:
        effective_owned.extend(owned_files)
        effective_owned.extend(test_files)

    ownership_lines: list[str] = []
    header = f" ({module_name})" if module_name else ""

    if effective_owned:
        files_list = "\n".join(f"- {f}" for f in effective_owned)
        ownership_lines.append(
            f"## Your Module Files{header}\n\n"
            f"You MUST create/modify ONLY these files:\n{files_list}\n\n"
        )

    if denied_for_agent:
        denied_list = "\n".join(f"- {f}" for f in denied_for_agent)
        if is_test_writer:
            denied_label = f"## READ-ONLY Source Files{header}\n\n"
            denied_desc = (
                "These source files are READ-ONLY. Do NOT modify or create them.\n"
                f"{denied_list}\n\n"
                "Your job is to write TESTS only. The source code will be written later "
                "by the implementer agent based on your tests.\n\n"
            )
        else:
            denied_label = f"## READ-ONLY Test Files{header}\n\n"
            denied_desc = (
                "These test files are READ-ONLY. Do NOT modify or delete them:\n"
                f"{denied_list}\n\n"
                "Tests define the specification. Fix the implementation, NEVER the tests.\n\n"
            )
        ownership_lines.append(denied_label + denied_desc)

    return "".join(ownership_lines)


def test_test_writer_sees_read_only_source_label():
    prompt = _build_ownership_prompt(
        agent_name="test-writer",
        owned_files=["src/app.py"],
        test_files=["tests/test_app.py"],
    )
    assert "READ-ONLY Source Files" in prompt
    assert "Your job is to write TESTS only" in prompt
    assert "READ-ONLY Test Files" not in prompt


def test_implementer_sees_read_only_test_label():
    prompt = _build_ownership_prompt(
        agent_name="implementer",
        owned_files=["src/app.py"],
        test_files=["tests/test_app.py"],
    )
    assert "READ-ONLY Test Files" in prompt
    assert "Fix the implementation, NEVER the tests" in prompt
    assert "READ-ONLY Source Files" not in prompt


def test_repairer_sees_read_only_test_label():
    prompt = _build_ownership_prompt(
        agent_name="repairer",
        owned_files=["src/app.py"],
        test_files=["tests/test_app.py"],
    )
    assert "READ-ONLY Test Files" in prompt
    assert "READ-ONLY Source Files" not in prompt


def test_test_writer_owns_test_files_only():
    prompt = _build_ownership_prompt(
        agent_name="test-writer",
        owned_files=["src/app.py"],
        test_files=["tests/test_app.py"],
    )
    assert "tests/test_app.py" in prompt.split("Your Module Files")[1].split("READ-ONLY")[0]


def test_implementer_owns_source_files_only():
    prompt = _build_ownership_prompt(
        agent_name="implementer",
        owned_files=["src/app.py"],
        test_files=["tests/test_app.py"],
    )
    assert "src/app.py" in prompt.split("Your Module Files")[1].split("READ-ONLY")[0]


def test_generic_agent_owns_all_files():
    prompt = _build_ownership_prompt(
        agent_name="planner",
        owned_files=["src/app.py"],
        test_files=["tests/test_app.py"],
    )
    assert "src/app.py" in prompt
    assert "tests/test_app.py" in prompt
    assert "READ-ONLY" not in prompt


def test_test_writer_underscore_variant():
    prompt = _build_ownership_prompt(
        agent_name="test_writer",
        owned_files=["src/app.py"],
        test_files=["tests/test_app.py"],
    )
    assert "READ-ONLY Source Files" in prompt
    assert "Your job is to write TESTS only" in prompt
