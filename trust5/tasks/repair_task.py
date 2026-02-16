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
