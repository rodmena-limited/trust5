import logging
import os
from stabilize import StageExecution, Task, TaskResult
from stabilize.errors import TransientError
from .agent import Agent
from .context_builder import build_implementation_prompt, discover_latest_spec
from .llm import LLM, LLMError
from .mcp_manager import mcp_clients
logger = logging.getLogger(__name__)

class ImplementerTask(Task):

    def execute(self, stage: StageExecution) -> TaskResult:
        project_root = os.getcwd()
        spec_id = stage.context.get("spec_id")

        if not spec_id:
            spec_id = discover_latest_spec(project_root)

        if not spec_id:
            return TaskResult.terminal(error="No SPEC found in .moai/specs/")

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
                if e.retryable or e.is_network_error:
                    retry_after = e.retry_after or (60 if e.is_network_error else 30)
                    raise TransientError(
                        f"LLM failed during implementation: {e}",
                        retry_after=retry_after,
                    )
                return TaskResult.terminal(error=f"Implementation LLM failed: {e}")
            except Exception as e:
                logger.exception("Implementation failed")
                return TaskResult.terminal(error=f"Implementation failed: {e}")
