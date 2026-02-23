import os

from rich.console import Console
from rich.prompt import Prompt

from .config import ConfigManager
from .lang import PROFILES, detect_language

console = Console()


class ProjectInitializer:
    """Interactive wizard that scaffolds a new Trust5 project directory."""

    def __init__(self, project_root: str = "."):
        self.project_root = project_root
        self.config_manager = ConfigManager(project_root)

    def run_wizard(self) -> None:
        console.print("[bold blue]Trust5 Project Initialization Wizard[/bold blue]")

        if os.path.exists(os.path.join(self.project_root, ".trust5", "config", "sections")):
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

    def _setup_structure(self) -> None:
        dirs = [
            ".trust5/config/sections",
            ".trust5/specs",
            ".trust5/project",
            ".trust5/memory/checkpoints",
            ".trust5/cache/loop-snapshots",
        ]
        for d in dirs:
            os.makedirs(os.path.join(self.project_root, d), exist_ok=True)

        self._create_file(
            ".trust5/project/product.md",
            "# Product Requirements\n\nTODO: Describe product vision.",
        )
        self._create_file(
            ".trust5/project/structure.md",
            "# System Architecture\n\nTODO: Describe system structure.",
        )
        self._create_file(".trust5/project/tech.md", "# Technology Stack\n\nTODO: Describe tech stack.")
        # Legacy config.json for compatibility — sits inside .trust5/
        import json

        config_data = {
            "quality": {"development_mode": "hybrid"},
            "project": {"name": "Trust5 Project"},
        }
        self._create_file(".trust5/config.json", json.dumps(config_data, indent=2))

        # Default MCP config:
        # - stabilize: Stabilize workflow engine (remote SSE, always available)
        # - docker: Docker MCP gateway (only if Docker Desktop + MCP Toolkit)
        default_mcp = {
            "mcpServers": {
                "stabilize": {
                    "transport": "sse",
                    "url": "https://mcp.stabilize.rodmena.ai/sse",
                },
                "docker": {
                    "command": "docker",
                    "args": ["mcp", "gateway", "run"],
                    "requireDocker": True,
                },
            }
        }
        self._create_file(".trust5/mcp.json", json.dumps(default_mcp, indent=2))

    def _create_file(self, path: str, content: str) -> None:
        full_path = os.path.join(self.project_root, path)
        if not os.path.exists(full_path):
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)

    def _write_default_config(self, language: str) -> None:
        config_path = os.path.join(self.project_root, ".trust5/config/sections")

        with open(os.path.join(config_path, "quality.yaml"), "w") as f:
            f.write("""development_mode: hybrid
coverage_threshold: 85.0
pass_score_threshold: 0.70
max_errors: 0
max_type_errors: 0
max_lint_errors: 0
max_warnings: 10
max_security_warnings: 0
max_quality_repairs: 3
max_jumps: 50
per_module_max_jumps: 30
max_repair_attempts: 5
max_reimplementations: 3
enforce_quality: true
""")

        with open(os.path.join(config_path, "git-strategy.yaml"), "w") as f:
            f.write("""auto_branch: true
branch_prefix: feature/
""")

        with open(os.path.join(config_path, "language.yaml"), "w") as f:
            f.write(f"""language: {language}
test_framework: {self._get_default_test_framework(language)}
""")

    @staticmethod
    def _get_default_test_framework(lang: str) -> str:
        from .lang import get_profile

        profile = get_profile(lang)
        if profile.test_command:
            return profile.test_command[0]
        return "auto"
