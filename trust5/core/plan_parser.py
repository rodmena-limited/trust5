from __future__ import annotations
import logging
import re
from dataclasses import dataclass
import yaml
logger = logging.getLogger(__name__)
_DEFAULT = PlanConfig()
_EARS_TAG_RE = re.compile(
    r"^\s*-\s*\[(UBIQ|EVENT|STATE|UNWNT|OPTNL|COMPLX)\]\s*(.+)",
    re.IGNORECASE,
)

def _parse_acceptance_criteria(raw: str) -> list[str]:
    """Extract EARS-tagged acceptance criteria from plan text."""
    criteria: list[str] = []
    for line in raw.splitlines():
        m = _EARS_TAG_RE.match(line.strip())
        if m:
            tag = m.group(1).upper()
            text = m.group(2).strip()
            criteria.append(f"[{tag}] {text}")
    return criteria

def _extract_block(raw: str, header: str) -> list[str]:
    """Robustly extract a text block following a header line."""
    lines = raw.splitlines()
    captured: list[str] = []
    in_block = False

    # Normalize header for matching (ignore case/spacing)
    norm_header = header.strip().lower()

    for line in lines:
        stripped = line.strip()
        if not in_block:
            if stripped.lower().startswith(norm_header):
                in_block = True
            continue

        # In block: stop at next section header (ALL CAPS followed by :)
        # but allow "## Header" or "### Header" styles too
        if stripped and stripped[0].isupper() and stripped.endswith(":") and " " not in stripped:
            break
        if stripped.startswith("##"):
            break

        # Skip empty lines at start, but keep them inside
        if not captured and not stripped:
            continue

        captured.append(line)

    return captured

def _parse_setup_commands(raw: str) -> list[str]:
    block_lines = _extract_block(raw, "SETUP_COMMANDS:")
    commands: list[str] = []

    for line in block_lines:
        stripped = line.strip()
        # Handle bullet points (- or *)
        if stripped.startswith(("- ", "* ")):
            cmd = stripped[2:].strip()
            if cmd:
                commands.append(cmd)
        # Handle numbered lists (1. 2.)
        elif re.match(r"^\d+\.\s", stripped):
            parts = stripped.split(" ", 1)
            if len(parts) > 1:
                commands.append(parts[1].strip())
        # Handle plain commands in code blocks (if user wrapped them)
        elif stripped and not stripped.startswith("```"):
            # Fallback: if it looks like a command (no spaces? no, commands have spaces)
            # Better to be strict about list format to avoid capturing prose
            pass

    return commands

@dataclass(frozen=True)
class PlanConfig:
    """Configuration extracted from the planner's output."""
    setup_commands: tuple[str, ...] = ()
    quality_threshold: float = 0.85
    test_command: str | None = None
    lint_command: str | None = None
    coverage_command: str | None = None
    acceptance_criteria: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "setup_commands": list(self.setup_commands),
            "quality_threshold": self.quality_threshold,
            "test_command": self.test_command,
            "lint_command": self.lint_command,
            "coverage_command": self.coverage_command,
            "acceptance_criteria": list(self.acceptance_criteria),
        }
