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

    def run_wizard(self) -> None:
        console.print("[bold blue]MoAI-ADK Project Initialization Wizard[/bold blue]")

        if os.path.exists(os.path.join(self.project_root, ".moai")):
            console.print("[yellow]Project already initialized.[/yellow]")
            return

        detected_lang = detect_language(self.project_root)
        choices = sorted(PROFILES.keys())
        language = Prompt.ask(
            "Select project language",
            choices=choices,
            default=detected_lang if detected_lang in choices else choices[0],
        )

        self._setup_structure()
        self._write_default_config(language)

        console.print("[green]Project initialized successfully![/green]")
