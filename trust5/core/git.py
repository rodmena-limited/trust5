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
