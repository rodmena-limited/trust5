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

    def init_repo(self) -> None:
        is_new = not os.path.exists(os.path.join(self.project_root, ".git"))
        if is_new:
            self._run_git(["init"])

        gitignore_path = os.path.join(self.project_root, ".gitignore")
        if not os.path.exists(gitignore_path):
            with open(gitignore_path, "w", encoding="utf-8") as f:
                f.write(_DEFAULT_GITIGNORE)

        if is_new:
            self._run_git(["add", "."])
            self._run_git(["commit", "-m", "chore: initial commit"])

    def create_worktree(self, branch_name: str, path: str) -> None:
        self._run_git(["worktree", "add", "-b", branch_name, path])

    def list_worktrees(self) -> list[dict[str, str]]:
        output = self._run_git(["worktree", "list", "--porcelain"])
        trees = []
        current: dict[str, str] = {}
        for line in output.splitlines():
            if not line:
                if current:
                    trees.append(current)
                    current = {}
                continue

            key, _, value = line.partition(" ")
            current[key] = value

        if current:
            trees.append(current)
        return trees

    def remove_worktree(self, path: str) -> None:
        self._run_git(["worktree", "remove", path])

    def commit(self, message: str, files: list[str] = ["."]) -> None:
        self._run_git(["add"] + files)
        self._run_git(["commit", "-m", message])
