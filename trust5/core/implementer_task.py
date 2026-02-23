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

logger = logging.getLogger(__name__)

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

        user_prompt = build_implementation_prompt(spec_id, project_root)
        system_prompt = self._load_system_prompt()

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
            except Exception as e:
                logger.exception("Implementation failed")
                return TaskResult.terminal(error=f"Implementation failed: {e}")

    @staticmethod
    def _load_system_prompt() -> str:
        base_path = os.path.dirname(os.path.abspath(__file__))
        prompt_path = os.path.join(base_path, "..", "assets", "prompts", "implementer.md")

        if not os.path.exists(prompt_path):
            return "You are a code implementer. Write complete, working code."

        try:
            with open(prompt_path, encoding="utf-8") as f:
                content = f.read()
        except Exception:
            logger.debug("Failed to read implementer prompt file", exc_info=True)
            return "You are a code implementer. Write complete, working code."

        if content.startswith("---\n"):
            parts = content.split("---\n", 2)
            if len(parts) >= 3:
                return parts[2]

        return content
