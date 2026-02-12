import logging
import os
from stabilize import StageExecution, Task, TaskResult
from stabilize.errors import TransientError
from .agent import Agent
from .context_builder import build_implementation_prompt, discover_latest_spec
from .llm import LLM, LLMError
from .mcp_manager import mcp_clients
logger = logging.getLogger(__name__)

class ImplementerTask(Task):
    pass
