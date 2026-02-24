"""EARS (Easy Approach to Requirements Syntax) patterns for planning phase."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class RequirementType(StrEnum):
    """EARS requirement classification types."""

    UBIQUITOUS = "ubiquitous"
    EVENT_DRIVEN = "event_driven"
    UNWANTED = "unwanted_behavior"
    STATE_DRIVEN = "state_driven"
    OPTIONAL = "optional"


@dataclass(frozen=True)
class EARSTemplate:
    """Frozen template for an EARS requirement pattern."""

    type: RequirementType
    name: str
    template: str
    description: str


TEMPLATES: dict[RequirementType, EARSTemplate] = {
    RequirementType.UBIQUITOUS: EARSTemplate(
        RequirementType.UBIQUITOUS,
        "Ubiquitous",
        "The <system> shall <response>.",
        "Always applies without preconditions.",
    ),
    RequirementType.EVENT_DRIVEN: EARSTemplate(
        RequirementType.EVENT_DRIVEN,
        "Event-Driven",
        "When <event>, the <system> shall <response>.",
        "Triggered by a specific event.",
    ),
    RequirementType.UNWANTED: EARSTemplate(
        RequirementType.UNWANTED,
        "Unwanted Behavior",
        "If <unwanted condition>, then the <system> shall <response>.",
        "Handles undesirable scenarios.",
    ),
    RequirementType.STATE_DRIVEN: EARSTemplate(
        RequirementType.STATE_DRIVEN,
        "State-Driven",
        "While <state>, the <system> shall <response>.",
        "Conditional on system state.",
    ),
    RequirementType.OPTIONAL: EARSTemplate(
        RequirementType.OPTIONAL,
        "Optional",
        "Where <feature>, the <system> shall <response>.",
        "Optional feature requirement.",
    ),
}


@dataclass
class Requirement:
    """Single EARS requirement with acceptance criteria."""

    type: RequirementType
    id: str
    description: str
    acceptance_criteria: list[str] = field(default_factory=list)

    def format(self) -> str:
        return f"[{self.id}] {self.description} (Type: {self.type.value})"

    def validate(self) -> str | None:
        if not self.id:
            return "requirement ID cannot be empty"
        if not self.description:
            return "requirement description cannot be empty"
        return None


class RequirementSet:
    """Indexed collection of validated EARS requirements."""

    def __init__(self) -> None:
        self._requirements: list[Requirement] = []
        self._index: dict[str, int] = {}

    def add(self, req: Requirement) -> str | None:
        err = req.validate()
        if err:
            return err
        if req.id in self._index:
            return f"duplicate ID: {req.id}"
        self._index[req.id] = len(self._requirements)
        self._requirements.append(req)
        return None

    def get(self, req_id: str) -> Requirement | None:
        idx = self._index.get(req_id)
        return self._requirements[idx] if idx is not None else None

    def filter_by_type(self, rt: RequirementType) -> list[Requirement]:
        return [r for r in self._requirements if r.type == rt]

    def all(self) -> list[Requirement]:
        return list(self._requirements)

    def __len__(self) -> int:
        return len(self._requirements)


def get_template(rt: RequirementType) -> EARSTemplate:
    return TEMPLATES[rt]


def all_templates() -> list[EARSTemplate]:
    return [TEMPLATES[rt] for rt in RequirementType]
