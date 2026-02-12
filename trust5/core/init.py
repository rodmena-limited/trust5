import os
from rich.console import Console
from rich.prompt import Prompt
from .config import ConfigManager
from .lang import PROFILES, detect_language
console = Console()

class ProjectInitializer:
    def __init__(self, project_root: str = "."):
        self.project_root = project_root
        self.config_manager = ConfigManager(project_root)
