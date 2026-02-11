import glob
import os
from typing import Any
MAX_FILE_CONTENT = 6000
MAX_TOTAL_CONTEXT = 30000
_FALLBACK_EXTENSIONS = (".py", ".go", ".ts", ".js", ".rs", ".java", ".rb")
_FALLBACK_SKIP_DIRS = (
    ".moai",
    ".trust5",
    ".git",
    "node_modules",
    "vendor",
    "__pycache__",
    ".venv",
    "venv",
    "target",
    "dist",
    "build",
)

def _read_file_safe(path: str, max_len: int = MAX_FILE_CONTENT) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        if len(content) > max_len:
            return content[:max_len] + f"\n... [{len(content) - max_len} chars truncated]"
        return content
    except Exception as e:
        return f"[Error reading {path}: {e}]"
