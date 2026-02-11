import os
import subprocess
_DEFAULT_GITIGNORE = """\
.trust5/
__pycache__/
*.py[cod]
*.egg-info/
dist/
build/
.venv/
venv/
node_modules/
.idea/
.vscode/
*.swp
*.swo
.DS_Store
Thumbs.db
.coverage
htmlcov/
.pytest_cache/
.env
.env.local
"""

class GitManager:
    def __init__(self, project_root: str = "."):
        self.project_root = project_root

    def _run_git(self, args: list[str]) -> str:
        try:
            result = subprocess.run(
                ["git"] + args,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Git command failed: git {' '.join(args)}\nError: {e.stderr}")
