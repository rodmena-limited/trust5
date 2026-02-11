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

def _detect_project_layout(project_root: str, language_profile: dict[str, Any]) -> str:
    """Detect project source layout and return a hint for the repair prompt."""
    source_roots = language_profile.get("source_roots", ())
    path_var = language_profile.get("path_env_var", "")
    manifest_files = language_profile.get("manifest_files", ())
    if not source_roots:
        return ""

    for root in source_roots:
        src_dir = os.path.join(project_root, root)
        if os.path.isdir(src_dir):
            lines = [
                f"PROJECT LAYOUT: Source code is in the '{root}/' subdirectory.",
            ]
            if path_var:
                lines.append(f"The test runner sets {path_var}={root} automatically.")
            # Check for missing manifest/package config
            has_manifest = (
                any(os.path.exists(os.path.join(project_root, mf)) for mf in manifest_files)
                if manifest_files
                else False
            )
            if not has_manifest:
                manifest_names = ", ".join(manifest_files) if manifest_files else "manifest file"
                lines.append(
                    f"WARNING: No package configuration found ({manifest_names}). "
                    f"If imports fail, the project may need a manifest file that "
                    f"configures the test runner to find modules in '{root}/'."
                )
            return "\n".join(lines)
    return ""

def build_repair_prompt(
    test_output: str,
    project_root: str,
    spec_id: str | None = None,
    attempt: int = 1,
    previous_failures: list[str] | None = None,
    language_profile: dict[str, Any] | None = None,
    plan_config: dict[str, Any] | None = None,
) -> str:
    lp = language_profile or {}
    extensions = tuple(lp.get("extensions", _FALLBACK_EXTENSIONS))
    skip_dirs = tuple(lp.get("skip_dirs", _FALLBACK_SKIP_DIRS))

    # Prefer the planner-provided test command (which includes venv activation)
    # over the generic profile default (which may not have venv context).
    plan_test_cmd = plan_config.get("test_command") if plan_config else None
    if plan_test_cmd:
        verify_cmd = f'Bash("{plan_test_cmd}")'
    else:
        verify_cmd = lp.get("test_verify_command", "the project's test command")

    source_files = _find_source_files(project_root, extensions)

    test_files_content = []
    source_files_content = []
    total_len = 0

    for fpath in source_files:
        if total_len >= MAX_TOTAL_CONTEXT:
            break
        rel = os.path.relpath(fpath, project_root)
        if "test" in rel or any(sd in rel for sd in skip_dirs):
            continue
        content = _read_file_safe(fpath)
        source_files_content.append(f"--- {rel} ---\n{content}")
        total_len += len(content)

    for fpath in source_files:
        if total_len >= MAX_TOTAL_CONTEXT:
            break
        rel = os.path.relpath(fpath, project_root)
        if "test" not in rel or any(sd in rel for sd in skip_dirs):
            continue
        content = _read_file_safe(fpath)
        test_files_content.append(f"--- {rel} ---\n{content}")
        total_len += len(content)

    spec_section = ""
    if spec_id:
        spec_section = f"\n\nSPEC CONTEXT:\n{build_spec_context(spec_id, project_root)}"

    previous_section = ""
    if previous_failures and len(previous_failures) > 0:
        prev_summary = "\n---\n".join(previous_failures[-3:])
        previous_section = f"""

PREVIOUS REPAIR ATTEMPTS (do NOT repeat the same fixes):
{prev_summary}
"""

    source_section = "\n\n".join(source_files_content) if source_files_content else "(no source files found)"
    test_section = "\n\n".join(test_files_content) if test_files_content else "(no test files found)"

    layout_section = _detect_project_layout(project_root, lp)
    if layout_section:
        layout_section = f"\n{layout_section}\n"

    return f"""REPAIR ATTEMPT {attempt}

WORKING DIRECTORY: {project_root}
WARNING: /testbed does NOT exist. All files are in {project_root}. Never reference /testbed.
{layout_section}
The following tests are FAILING. Your job is to fix the SOURCE CODE (not the tests)
so that ALL tests pass.

TEST OUTPUT:
{test_output[:4000]}

SOURCE FILES:
{source_section}

TEST FILES:
{test_section}
{spec_section}
{previous_section}

REPAIR RULES:
1. Read the failing test to understand what it EXPECTS.
2. Read the source code to understand what it DOES.
3. Fix the source code to match test expectations.
4. NEVER modify test files. Only fix implementation files.
5. After fixing, run: {verify_cmd} to verify your fixes work.
6. If tests still fail, keep fixing until they pass.
7. Focus on the ROOT CAUSE, not symptoms.
8. If dependencies are missing, use the project's virtual environment to install them.
9. STOP IMMEDIATELY after all tests pass — return your summary.
"""
