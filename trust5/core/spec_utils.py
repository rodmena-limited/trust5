# DEPRECATED: This module is kept for backward compatibility.
# Use trust5.core.context_builder instead.

from .context_builder import (  # noqa: F401
    build_implementation_prompt,
    build_project_context,
    build_repair_prompt,
    build_spec_context,
    discover_latest_spec,
)
