import os
from rich.console import Console
from rich.prompt import Prompt
from .config import ConfigManager
from .lang import PROFILES, detect_language
console = Console()
