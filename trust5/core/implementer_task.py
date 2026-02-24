import logging
import os
from datetime import timedelta

from resilient_circuit import ExponentialDelay
from stabilize import StageExecution, Task, TaskResult
from stabilize.errors import TransientError

from .agent import Agent
from .context_builder import build_implementation_prompt, discover_latest_spec
from .llm import LLM, LLMError
from .mcp_manager import mcp_clients
from .message import M, emit

logger = logging.getLogger(__name__)

# Source file extensions to clean during rebuild.
_SOURCE_EXTS = frozenset(
    {
        ".py",
        ".go",
        ".ts",
        ".js",
        ".tsx",
        ".jsx",
        ".rs",
        ".java",
        ".rb",
        ".ex",
        ".exs",
        ".cpp",
        ".c",
        ".h",
    }
)

# Directories to skip during rebuild cleanup.
_SKIP_DIRS = frozenset(
    {
        ".trust5",
        ".moai",
        ".git",
        "node_modules",
        "vendor",
        "__pycache__",
        ".venv",
        "venv",
        "target",
        "dist",
        "build",
        ".tox",
        ".nox",
    }
)

# Outer (Stabilize-level) retry backoff for LLM errors.
# Inner retry in LLM._chat_with_retry already spent its budget.
_OUTER_BACKOFF_CONNECTION = ExponentialDelay(
    min_delay=timedelta(seconds=120),
    max_delay=timedelta(seconds=300),
    factor=2,
    jitter=0.3,
)
_OUTER_BACKOFF_DEFAULT = ExponentialDelay(
    min_delay=timedelta(seconds=60),
    max_delay=timedelta(seconds=300),
    factor=2,
    jitter=0.3,
)


class ImplementerTask(Task):
    """Stabilize task that implements source code from a SPEC document.

    Loads the latest SPEC, builds an implementation prompt with ancestor
    context and project structure, then runs an LLM agent to write code.
    Handles rebuild signals and connection errors with exponential backoff.
    """

    def execute(self, stage: StageExecution) -> TaskResult:
        """Execute the implementation task within the Stabilize workflow."""
        project_root = os.getcwd()
        spec_id = stage.context.get("spec_id")

        if not spec_id:
            spec_id = discover_latest_spec(project_root)

        if not spec_id:
            return TaskResult.terminal(error="No SPEC found in .trust5/specs/")

        logger.info("Implementing %s", spec_id)

        # ── Rebuild handling ──────────────────────────────────────────
        rebuild_requested = stage.context.get("_rebuild_requested", False)
        rebuild_reason = stage.context.get("_rebuild_reason", "")
        if rebuild_requested:
            self._clean_source_files(project_root, stage.context)
            emit(M.WDWN, f"Rebuild: cleaned source files. Reason: {rebuild_reason}")
            logger.info("Rebuild: cleaned source files. Reason: %s", rebuild_reason)

        base_prompt = build_implementation_prompt(spec_id, project_root)
        system_prompt = self._load_system_prompt()

        # Inject rebuild context into the user prompt so the LLM knows
        # why the rebuild was triggered and what to avoid.
        if rebuild_requested:
            rebuild_preamble = (
                "\n\n## REBUILD NOTICE\n"
                "The watchdog has ordered a FULL REBUILD of this project.\n"
                f"Reason: {rebuild_reason}\n"
                "Previous implementation attempts failed repeatedly.\n"
                "You MUST write the code from scratch. Do NOT repeat previous mistakes.\n"
                "All source files have been deleted. Only test files and config remain.\n"
            )
            user_prompt = rebuild_preamble + "\n" + base_prompt
        else:
            user_prompt = base_prompt

        llm = LLM.for_tier("best", stage_name="implementer")

        with mcp_clients() as mcp:
            agent = Agent(
                name="implementer",
                prompt=system_prompt,
                llm=llm,
                non_interactive=True,
                mcp_clients=mcp,
            )

            try:
                result = agent.run(user_prompt, max_turns=25)
                return TaskResult.success(
                    outputs={
                        "result": result,
                        "spec_id": spec_id,
                        "project_root": project_root,
                    }
                )
            except LLMError as e:
                if e.is_auth_error or e.retryable or e.is_network_error:
                    outer_attempt = stage.context.get("_transient_retry_count", 0) + 1
                    stage.context["_transient_retry_count"] = outer_attempt
                    if e.is_auth_error or e.is_network_error:
                        retry_after = _OUTER_BACKOFF_CONNECTION.for_attempt(outer_attempt)
                    else:
                        retry_after = max(e.retry_after, _OUTER_BACKOFF_DEFAULT.for_attempt(outer_attempt))
                    raise TransientError(
                        f"LLM failed during implementation: {e}",
                        retry_after=retry_after,
                    )
                return TaskResult.terminal(error=f"Implementation LLM failed: {e}")
            except (OSError, RuntimeError, ValueError, KeyError) as e:  # implementation: non-LLM errors
                logger.exception("Implementation failed")
                return TaskResult.terminal(error=f"Implementation failed: {e}")

    @staticmethod
    def _clean_source_files(project_root: str, context: dict) -> None:
        """Delete source files before a full rebuild.

        Removes all source files (by extension) from the project directory,
        preserving test files, config, .trust5/, .moai/, and .git/.
        If ``owned_files`` is in context, only those files are deleted.
        Otherwise, all source files outside skip dirs are removed.
        """
        owned_files = context.get("owned_files")
        deleted = 0

        if owned_files and isinstance(owned_files, (list, tuple)):
            # Scoped rebuild: only delete owned source files
            for fpath in owned_files:
                full = os.path.join(project_root, fpath) if not os.path.isabs(fpath) else fpath
                if os.path.isfile(full):
                    try:
                        os.remove(full)
                        deleted += 1
                    except OSError:
                        logger.debug("Failed to delete owned file: %s", fpath)
        else:
            # Full rebuild: delete all source files outside protected dirs
            for dirpath, dirnames, filenames in os.walk(project_root):
                # Prune protected directories
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
                for fname in filenames:
                    _, ext = os.path.splitext(fname)
                    if ext.lower() not in _SOURCE_EXTS:
                        continue
                    # Preserve test files
                    lower = fname.lower()
                    if lower.startswith("test_") or "_test" in lower or lower.startswith("test."):
                        continue
                    full = os.path.join(dirpath, fname)
                    try:
                        os.remove(full)
                        deleted += 1
                    except OSError:
                        logger.debug("Failed to delete source file: %s", full)

        logger.info("Rebuild cleanup: deleted %d source files", deleted)

    @staticmethod
    def _load_system_prompt() -> str:
        base_path = os.path.dirname(os.path.abspath(__file__))
        prompt_path = os.path.join(base_path, "..", "assets", "prompts", "implementer.md")

        if not os.path.exists(prompt_path):
            return "You are a code implementer. Write complete, working code."

        try:
            with open(prompt_path, encoding="utf-8") as f:
                content = f.read()
        except OSError:  # prompt file read error
            logger.debug("Failed to read implementer prompt file", exc_info=True)
            return "You are a code implementer. Write complete, working code."

        if content.startswith("---\n"):
            parts = content.split("---\n", 2)
            if len(parts) >= 3:
                return parts[2]

        return content
