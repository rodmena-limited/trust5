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
