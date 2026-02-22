"""Shared context keys propagated between stages via jump_to.

Having a single source of truth prevents drift when adding new context keys
that need to survive validate → repair → validate loops.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAX_JUMPS = 50

# Keys that must be propagated across stage jumps (validate/repair/quality loops)
PROPAGATED_CONTEXT_KEYS: tuple[str, ...] = (
    "jump_repair_ref",
    "jump_validate_ref",
    "jump_implement_ref",
    "jump_quality_ref",
    "test_files",
    "owned_files",
    "module_name",
    "plan_config",
    "repair_attempt",
    "_max_jumps",
    "_jump_count",
    "cross_module_tests",
)


def propagate_context(
    source: dict[str, Any],
    target: dict[str, Any],
    keys: tuple[str, ...] = PROPAGATED_CONTEXT_KEYS,
) -> None:
    """Copy non-None values from source to target for the given keys."""
    for key in keys:
        val = source.get(key)
        if val is not None:
            target[key] = val


def increment_jump_count(context: dict[str, Any]) -> int:
    """Increment and return the jump counter.  Must be called before every jump_to."""
    count = context.get("_jump_count", 0) + 1
    context["_jump_count"] = count
    return count


def check_jump_limit(context: dict[str, Any]) -> bool:
    """Return True if the jump limit has been exceeded."""
    count = context.get("_jump_count", 0)
    limit = context.get("_max_jumps", DEFAULT_MAX_JUMPS)
    if count >= limit:
        logger.warning(
            "Jump limit reached: %d/%d jumps used",
            count,
            limit,
        )
        return True
    return False
