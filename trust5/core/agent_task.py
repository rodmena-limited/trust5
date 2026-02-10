import logging
import os
import threading
import time
from typing import Any
import yaml
from stabilize import StageExecution, Task, TaskResult
from stabilize.errors import TransientError
from ..core.agent import Agent
from ..core.context_builder import build_project_context
from ..core.ears import all_templates
from ..core.lang import LanguageProfile, build_language_context
from ..core.llm import LLM, LLMError
from ..core.mcp_manager import mcp_clients
from ..core.message import M, emit
logger = logging.getLogger(__name__)
NON_INTERACTIVE_PREFIX = (
    "CRITICAL: You are running inside a fully autonomous, non-interactive pipeline. "
    "There is NO human at the terminal. You MUST make all decisions autonomously "
    "using sensible defaults. NEVER attempt to ask the user questions — the "
    "AskUserQuestion tool is NOT available. If you need to make a choice, pick the "
    "most reasonable option and proceed.\n\n"
    "IMPORTANT: /testbed does NOT exist. NEVER read, write, or cd to /testbed. "
    "All file paths must be relative to the current working directory.\n\n"
)
