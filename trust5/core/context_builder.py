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

def _find_source_files(
    project_root: str,
    extensions: tuple[str, ...] = _FALLBACK_EXTENSIONS,
) -> list[str]:
    found = []
    for ext in extensions:
        pattern = f"**/*{ext}"
        found.extend(glob.glob(os.path.join(project_root, pattern), recursive=True))
    return sorted(set(found))

def build_spec_context(spec_id: str, project_root: str) -> str:
    spec_dir = os.path.join(project_root, ".moai", "specs", spec_id)
    parts = []
    for fname in ["spec.md", "plan.md", "acceptance.md"]:
        fpath = os.path.join(spec_dir, fname)
        if os.path.exists(fpath):
            content = _read_file_safe(fpath)
            parts.append(f"--- {fname} ---\n{content}")
    if not parts:
        return f"(No SPEC files found for {spec_id})"
    return "\n\n".join(parts)

def build_implementation_prompt(
    spec_id: str,
    project_root: str,
    language_profile: dict[str, Any] | None = None,
) -> str:
    spec_content = build_spec_context(spec_id, project_root)
    lp = language_profile or {}
    verify_cmd = lp.get("test_verify_command", "the project's test command")

    return f"""You are implementing {spec_id}.

Below is the full SPEC content. Read it carefully and then IMPLEMENT the code.

{spec_content}

---

IMPLEMENTATION RULES:
1. Use the Write tool to create ALL source code files.
2. Create complete, production-quality, working code with proper error handling.
3. Create comprehensive tests alongside source code using the project's test framework.
4. After writing all files, run: {verify_cmd} to verify tests pass.
5. If any tests fail, read the failure output, fix the code, and re-run until ALL tests pass.
6. Use Glob to verify all files exist on disk.
7. Do NOT ask any questions. Implement with sensible defaults.
8. Do NOT use AskUserQuestion.
9. Every public function must have at least one test.
10. Handle edge cases that the acceptance criteria describe.
"""
