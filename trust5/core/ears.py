from __future__ import annotations
from dataclasses import dataclass, field
from enum import StrEnum
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

class RequirementType(StrEnum):
    UBIQUITOUS = 'ubiquitous'
    EVENT_DRIVEN = 'event_driven'
    UNWANTED = 'unwanted_behavior'
    STATE_DRIVEN = 'state_driven'
    OPTIONAL = 'optional'
