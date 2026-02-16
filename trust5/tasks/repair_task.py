import logging
import os
import subprocess
from typing import Any
from stabilize import StageExecution, Task, TaskResult
from stabilize.errors import TransientError
from ..core.agent import Agent
from ..core.context_builder import build_repair_prompt
from ..core.context_keys import check_jump_limit, increment_jump_count, propagate_context
from ..core.error_summarizer import summarize_errors
from ..core.lang import LanguageProfile, build_language_context, detect_language, get_profile
from ..core.llm import LLM, LLMError
from ..core.mcp_manager import mcp_clients
from ..core.message import M, emit
logger = logging.getLogger(__name__)
REPAIR_SYSTEM_PROMPT_FILE = "repairer.md"

def _build_test_env(
    project_root: str,
    profile_data: dict[str, Any],
) -> dict[str, str] | None:
    """Build subprocess env with source roots on the language path var."""
    source_roots = profile_data.get("source_roots", ())
    path_var = profile_data.get("path_env_var", "")
    if not source_roots or not path_var:
        return None

    for root in source_roots:
        src_dir = os.path.join(project_root, root)
        if os.path.isdir(src_dir):
            env = os.environ.copy()
            existing = env.get(path_var, "")
            env[path_var] = f"{src_dir}:{existing}" if existing else src_dir
            return env

    return None

class RepairTask(Task):
    """Runs an LLM agent to fix code based on test failures, then jumps back to validate."""
