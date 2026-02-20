import logging
import os
import subprocess
from typing import Any

from stabilize import StageExecution, Task, TaskResult
from stabilize.errors import TransientError

from ..core.agent import Agent
from ..core.context_builder import build_repair_prompt
from ..core.context_keys import check_jump_limit, increment_jump_count, propagate_context
from ..core.error_summarizer import summarize_errors
from ..core.lang import LanguageProfile, build_language_context, detect_language, get_profile
from ..core.llm import LLM, LLMError
from ..core.mcp_manager import mcp_clients
from ..core.message import M, emit

logger = logging.getLogger(__name__)

REPAIR_SYSTEM_PROMPT_FILE = "repairer.md"


class RepairTask(Task):
    """Runs an LLM agent to fix code based on test failures, then jumps back to validate."""

    def execute(self, stage: StageExecution) -> TaskResult:
        project_root = stage.context.get("project_root", os.getcwd())
        test_output = stage.context.get("test_output", "")
        repair_attempt = stage.context.get("repair_attempt", 1)
        spec_id = stage.context.get("spec_id")
        previous_failures = stage.context.get("previous_failures", [])
        profile_data = stage.context.get("language_profile", {})

        # ── Jump-limit safety net ─────────────────────────────────────
        if check_jump_limit(stage.context):
            emit(
                M.RFAL,
                f"Jump limit reached "
                f"({stage.context.get('_jump_count', 0)}/{stage.context.get('_max_jumps', 50)}). "
                f"Marking as failed — pipeline continues with other modules.",
            )
            return TaskResult.failed_continue(
                error="Jump limit exceeded — validate/repair loop ran too long",
                outputs={"tests_passed": False, "jump_limit_reached": True},
            )

        # Re-detect language at runtime when build-time detection was "unknown"
        # (pipeline created before setup stage ran and created manifest files).
        if profile_data.get("language") == "unknown":
            detected = detect_language(project_root)
            if detected != "unknown":
                profile_data = get_profile(detected).to_dict()
                stage.context["language_profile"] = profile_data

        failure_type = stage.context.get("failure_type")
        tests_passed = stage.context.get("tests_passed", False)
        tests_partial = stage.context.get("tests_partial", False)
        # Use get() instead of pop(): pop() is destructive and breaks crash
        # recovery (Stabilize replays stages from events — if the key was
        # already popped, the retry silently skips repair).
        repair_requested = stage.context.get("_repair_requested", False)

        if not repair_requested:
            emit(
                M.RSKP,
                "Repair started via DAG (not jump). No active failure. Skipping.",
            )
            return TaskResult.success(outputs={"repair_skipped": True})

        # Quality failures need repair even when tests pass — skip logic
        # only applies to test-driven repairs.
        # When repair_requested is True, validate used jump_to to reach us,
        # so we must jump back to validate (not return success) to ensure
        # validate's stage.completed event fires and resolves downstream.
        if failure_type != "quality":
            if not test_output or tests_passed or tests_partial:
                emit(
                    M.RSKP,
                    "No failures to repair (tests_passed, tests_partial, or no test_output). Skipping.",
                )
                jump_target = stage.context.get("jump_validate_ref", "validate")
                skip_context: dict[str, Any] = {
                    "project_root": project_root,
                    "language_profile": profile_data,
                    "_repair_requested": False,
                }
                propagate_context(stage.context, skip_context)
                increment_jump_count(skip_context)
                return TaskResult.jump_to(
                    jump_target,
                    context=skip_context,
                    outputs={"repair_skipped": True},
                )

        module_name = stage.context.get("module_name", "")
        mod_tag = f" [{module_name}]" if module_name else ""

        # ── Pre-flight check ────────────────────────────────────────
        # Guard against the validate→repair→validate infinite loop.
        # The _repair_requested flag persists in Stabilize's sticky stage
        # context across re-executions, so even when validate SUCCEEDS
        # (DAG continues to repair), the flag is still True from a
        # previous jump. Running a quick test check catches this case.
        #
        # IMPORTANT: We must jump back to validate even when tests pass,
        # never return TaskResult.success() directly. When validate used
        # jump_to("repair"), its stage.completed event was never emitted.
        # Only by jumping back to validate can it re-execute, succeed,
        # and emit stage.completed — which is the ONLY path that resolves
        # downstream dependencies in the DAG.
        #
        # Pre-flight ONLY for test failures.  For lint/syntax failures the
        # repair LLM must run (tests passing says nothing about lint).
        # Running a test-only pre-flight on lint failures causes an infinite
        # skip loop: validate(lint fail) → repair(tests pass → skip) → repeat.
        if failure_type in ("test", None):
            pre_check = self._quick_test_check(project_root, profile_data, stage.context)
            if pre_check:
                emit(
                    M.RSKP,
                    f"Pre-flight check: tests already passing{mod_tag}. No repair needed.",
                )
                jump_target = stage.context.get("jump_validate_ref", "validate")
                emit(M.RJMP, f"Jumping back to {jump_target} to resolve downstream stages.")
                preflight_context: dict[str, Any] = {
                    "project_root": project_root,
                    "previous_failures": previous_failures,
                    "spec_id": spec_id,
                    "language_profile": profile_data,
                    "_repair_requested": False,
                }
                propagate_context(stage.context, preflight_context)
                increment_jump_count(preflight_context)
                return TaskResult.jump_to(
                    jump_target,
                    context=preflight_context,
                    outputs={"repair_skipped": True, "tests_already_pass": True},
                )

        emit(
            M.RSTR,
            f"RepairTask executing{mod_tag} (attempt {repair_attempt}) in {project_root}",
            label=module_name,
        )

        summarized_output = summarize_errors(
            test_output,
            failure_type=failure_type or "test",
        )

        system_prompt = self._load_repairer_prompt(profile_data)

        # Append the "Project Language" section that the repairer.md references
        # (e.g., "run the test command from the Project Language section").
        # AgentTask does this automatically, but RepairTask builds its own Agent.
        if profile_data:
            try:
                lang_profile = LanguageProfile(**profile_data)
                system_prompt += "\n\n" + build_language_context(lang_profile)
            except (TypeError, KeyError):
                pass

        plan_config = stage.context.get("plan_config")
        user_prompt = build_repair_prompt(
            test_output=summarized_output,
            project_root=project_root,
            spec_id=spec_id,
            attempt=repair_attempt,
            previous_failures=previous_failures,
            language_profile=profile_data,
            plan_config=plan_config,
        )

        owned_files = stage.context.get("owned_files")
        test_files = stage.context.get("test_files")

        repair_thinking = "high" if repair_attempt >= 2 else "low"
        llm = LLM.for_tier("best", stage_name="repairer", thinking_level=repair_thinking)

        with mcp_clients() as mcp:
            agent = Agent(
                name="repairer",
                prompt=system_prompt,
                llm=llm,
                non_interactive=True,
                owned_files=owned_files,
                denied_files=test_files,
                deny_test_patterns=True,
                mcp_clients=mcp,
            )

            try:
                result = agent.run(user_prompt, max_turns=20, timeout_seconds=600)

                # Always jump back to validate (or quality) — never short-circuit
                # with TaskResult.success() from repair.  When repair returns
                # success directly, the source stage (validate) that used jump_to
                # to reach repair never completes through Stabilize's normal
                # CompleteStageHandler.  That handler is the ONLY path that
                # resolves downstream dependencies.  Returning success here
                # leaves all downstream stages permanently NOT_STARTED.
                # Validate re-runs in <1 s and handles the success path correctly.
                if failure_type == "quality":
                    jump_target = stage.context.get("jump_quality_ref", "quality")
                else:
                    jump_target = stage.context.get("jump_validate_ref", "validate")
                emit(M.REND, f"Repair work done{mod_tag}.", label=module_name)
                emit(M.RJMP, f"Repair completed. Jumping back to {jump_target}.")

                jump_context: dict[str, Any] = {
                    "project_root": project_root,
                    "previous_failures": previous_failures,
                    "spec_id": spec_id,
                    "last_repair_summary": result[:500] if result else "",
                    "language_profile": profile_data,
                    # Preserve repair_attempt so validate sees the correct count
                    "repair_attempt": repair_attempt,
                    # Explicitly clear the repair flag so validate doesn't
                    # see a stale True from sticky context after a successful repair.
                    "_repair_requested": False,
                }
                if failure_type == "quality":
                    jump_context["quality_attempt"] = stage.context.get("quality_attempt", 1)
                    jump_context["max_quality_attempts"] = stage.context.get("max_quality_attempts", 3)
                    jump_context["prev_quality_report"] = stage.context.get("prev_quality_report")
                    jump_context["pipeline_phase"] = stage.context.get("pipeline_phase", "run")

                propagate_context(stage.context, jump_context)
                increment_jump_count(jump_context)

                return TaskResult.jump_to(
                    jump_target,
                    context=jump_context,
                    outputs={"repair_result": result[:2000] if result else ""},
                )

            except LLMError as e:
                if e.is_auth_error or e.retryable or e.is_network_error:
                    retry_after = 120.0 if e.is_auth_error else (e.retry_after or (60 if e.is_network_error else 30))
                    emit(
                        M.RFAL,
                        f"Repair LLM transient failure, retrying in {retry_after}s: {e}",
                    )
                    raise TransientError(
                        f"LLM failed during repair: {e}",
                        retry_after=retry_after,
                        context_update={
                            "previous_failures": previous_failures,
                        },
                    )
                emit(M.RFAL, f"Repair LLM failed permanently: {e}")
                return TaskResult.terminal(error=f"Repair LLM failed permanently: {e}")

            except Exception as e:
                emit(M.RFAL, f"RepairTask failed: {e}")
                logger.exception("RepairTask failed")
                return TaskResult.terminal(error=f"Repair failed: {e}")

    @staticmethod
    def _quick_test_check(
        project_root: str,
        profile_data: dict[str, Any],
        stage_context: dict[str, Any],
    ) -> bool:
        """Run a quick test to see if tests are currently passing.

        Returns True if all tests pass, False otherwise.
        Used for pre-flight (skip repair if nothing is broken) and
        post-flight (avoid jumping back to validate if repair succeeded).
        """
        # Use plan_config test_command if available (matches ValidateTask behavior).
        # Import here to avoid circular dependency.
        from ..tasks.validate_task import _parse_command

        plan_config = stage_context.get("plan_config", {})
        plan_test_cmd = plan_config.get("test_command") if plan_config else None
        if plan_test_cmd:
            test_cmd = _parse_command(plan_test_cmd)
        else:
            detected = detect_language(project_root)
            if detected == "unknown" and not profile_data.get("test_command"):
                # No real test command available (generic profile uses `echo`
                # which always returns 0).  Can't do a meaningful pre-flight.
                return False
            base_profile = get_profile(detected)
            test_cmd = tuple(profile_data.get("test_command", base_profile.test_command))

        # Scope to module test files if available
        scoped_test_files = stage_context.get("test_files")
        if scoped_test_files:
            existing = [f for f in scoped_test_files if os.path.exists(os.path.join(project_root, f))]
            if existing:
                test_cmd = (*test_cmd, *existing)

        # Inject per-test timeout for pytest (matches ValidateTask behavior)
        from ..core.constants import PYTEST_PER_TEST_TIMEOUT

        lang_name = profile_data.get("language", "")
        if lang_name == "python" and any("pytest" in str(t) for t in test_cmd):
            if not any("--timeout" in str(t) for t in test_cmd):
                test_cmd = (*test_cmd, f"--timeout={PYTEST_PER_TEST_TIMEOUT}")

        # Build env with source roots on path (matches ValidateTask behavior)
        env = _build_test_env(project_root, profile_data)

        try:
            result = subprocess.run(
                list(test_cmd),
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )
            # Self-heal: if --timeout isn't recognized, retry without it.
            if (
                result.returncode != 0
                and "unrecognized arguments: --timeout" in (result.stderr or "")
            ):
                cleaned = [t for t in test_cmd if not t.startswith("--timeout")]
                if len(cleaned) < len(list(test_cmd)):
                    result = subprocess.run(
                        cleaned,
                        cwd=project_root,
                        capture_output=True,
                        text=True,
                        timeout=60,
                        env=env,
                    )
            return result.returncode == 0
        except Exception as e:
            logger.debug("Pre/post-flight test check failed: %s", e)
            return False

    def _load_repairer_prompt(self, profile_data: dict[str, Any] | None = None) -> str:
        base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        prompt_path = os.path.join(base_path, "assets", "prompts", REPAIR_SYSTEM_PROMPT_FILE)

        if not os.path.exists(prompt_path):
            return self._default_repairer_prompt(profile_data)

        try:
            with open(prompt_path, encoding="utf-8") as f:
                content = f.read()
        except Exception:
            return self._default_repairer_prompt(profile_data)

        if content.startswith("---\n"):
            parts = content.split("---\n", 2)
            if len(parts) >= 3:
                return parts[2]
        return content

    @staticmethod
    def _default_repairer_prompt(profile_data: dict[str, Any] | None = None) -> str:
        verify_cmd = "the project's test command"
        if profile_data:
            verify_cmd = profile_data.get("test_verify_command", verify_cmd)
        return (
            "You are a code repair agent. Fix the source code to make failing "
            "tests pass. Never modify test files. Read the test, understand "
            "what it expects, fix the implementation. After fixing, run "
            f"{verify_cmd} to verify."
        )


def _build_test_env(
    project_root: str,
    profile_data: dict[str, Any],
) -> dict[str, str] | None:
    """Build subprocess env with source roots on the language path var."""
    source_roots = profile_data.get("source_roots", ())
    path_var = profile_data.get("path_env_var", "")
    if not source_roots or not path_var:
        return None

    for root in source_roots:
        src_dir = os.path.join(project_root, root)
        if os.path.isdir(src_dir):
            env = os.environ.copy()
            existing = env.get(path_var, "")
            env[path_var] = f"{src_dir}:{existing}" if existing else src_dir
            return env

    return None
