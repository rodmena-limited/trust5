import os
import subprocess
import time
from typing import Any
from stabilize import StageExecution, Task, TaskResult
from ..core.agent import Agent
from ..core.config import ConfigManager
from ..core.lang import detect_language, get_profile
from ..core.llm import LLM
from ..core.lsp import LSPClient
from ..core.mcp_manager import mcp_clients
from ..core.message import M, emit

class RalphLoop:
    def __init__(self, project_root: str):
        self.project_root = project_root
        self.config_manager = ConfigManager(project_root)
        self.config = self.config_manager.load_config()

        language = detect_language(project_root)
        self.profile = get_profile(language)

        lsp_cmd = self.config.language.lsp_command
        self.lsp = LSPClient(lsp_cmd, root_uri=f"file://{os.path.abspath(project_root)}")

        self.max_iterations = 100
        self.iteration_count = 0
