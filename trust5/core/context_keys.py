from __future__ import annotations
import logging
from typing import Any
logger = logging.getLogger(__name__)
DEFAULT_MAX_JUMPS = 50
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
)
