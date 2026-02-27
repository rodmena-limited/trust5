import logging
import os
import subprocess
from datetime import timedelta
from typing import Any

from resilient_circuit import ExponentialDelay
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
from .watchdog_task import check_rebuild_signal, clear_rebuild_signal

logger = logging.getLogger(__name__)

# Outer (Stabilize-level) retry backoff for LLM errors.
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

        # ── Watchdog rebuild signal ───────────────────────────────────
        rebuild_signaled, rebuild_reason = check_rebuild_signal(project_root)
        if rebuild_signaled:
            clear_rebuild_signal(project_root)
            emit(M.RFAL, f"Watchdog ordered rebuild: {rebuild_reason}")
            rebuild_ctx: dict[str, Any] = {
                "_rebuild_requested": True,
                "_rebuild_reason": rebuild_reason,
            }
            propagate_context(stage.context, rebuild_ctx)
            increment_jump_count(rebuild_ctx)
            return TaskResult.jump_to(
                stage.context.get("jump_validate_ref", "validate"),
                context=rebuild_ctx,
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

        # Inject watchdog audit findings so the repairer is aware of
        # file-system anomalies (garbled files, missing manifests, stubs).
        from .watchdog_task import load_watchdog_findings

        watchdog_ctx = load_watchdog_findings(project_root)
        if watchdog_ctx:
            system_prompt += "\n\n" + watchdog_ctx

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

        # Detect missing/stub source files and prepend creation guidance.
        # When owned source files don't exist (or contain only scaffold stubs),
        # the root cause is that implementation never happened — not a bug to
        # fix.  Tell the repairer explicitly so it creates files from scratch.
        if owned_files:
            missing_src: list[str] = []
            stub_src: list[str] = []
            for src_file in owned_files:
                src_full = os.path.join(project_root, src_file)
                if not os.path.exists(src_full):
                    missing_src.append(src_file)
                else:
                    try:
                        with open(src_full, encoding="utf-8") as fh:
                            content = fh.read().strip()
                        if len(content) < 100:
                            lower = content.lower()
                            if "implementation required" in lower:
                                stub_src.append(src_file)
                            elif not content or (content.startswith('"""') and content.endswith('"""')):
                                stub_src.append(src_file)
                    except OSError:
                        pass

            if missing_src or stub_src:
                guidance_parts: list[str] = [
                    "## CRITICAL: Source Files Need Implementation\n\n"
                    "The test failures are caused by missing source code, not bugs. "
                    "You MUST use the Write tool to create these files with COMPLETE implementations.\n\n"
                ]
                if missing_src:
                    guidance_parts.append(f"Files that DO NOT EXIST (must be created): {missing_src}\n")
                if stub_src:
                    guidance_parts.append(f"Files that are EMPTY STUBS (must be replaced with real code): {stub_src}\n")
                guidance_parts.append(
                    "\nDo NOT try to extract or copy code from other modules' files. "
                    "Write your implementation independently based on what the tests expect.\n\n"
                )
                user_prompt = "".join(guidance_parts) + user_prompt

        # Cross-module interface hints: when errors involve TypeError or
        # ImportError for classes/functions from OTHER modules, guide the
        # repair agent to read the calling code and adapt its interface.
        if owned_files and test_output:
            cross_mod_hint = _build_cross_module_hint(test_output, owned_files)
            if cross_mod_hint:
                user_prompt = cross_mod_hint + user_prompt

        llm = LLM.for_tier("best", stage_name="repairer")

        # Integration repair (no owned_files) operates across all modules.
        # It must be able to fix test infrastructure (imports, fixtures)
        # to resolve cross-module mismatches, so test patterns are NOT denied.
        # Per-module repair still denies test patterns (tests are the spec).
        is_integration = owned_files is None
        deny_tests = not is_integration

        # Integration repair: override the "NO TEST MODIFICATION" prohibition.
        # Integration has no owned_files and needs to fix cross-module issues
        # including test infrastructure (imports, fixtures, conftest).
        if is_integration:
            system_prompt += (
                "\n\n## Integration Mode Override\n\n"
                "You are running in INTEGRATION mode (cross-module repair). "
                "The normal 'NO TEST MODIFICATION' rule is RELAXED:\n"
                "- You MAY fix test **imports**, **fixtures**, and **conftest** files.\n"
                "- You MUST NOT change test **assertions** or **expected values**.\n"
                "- Check all test files for inconsistent import paths and normalize them.\n"
            )

        with mcp_clients() as mcp:
            agent = Agent(
                name="repairer",
                prompt=system_prompt,
                llm=llm,
                non_interactive=True,
                owned_files=owned_files,
                denied_files=test_files if deny_tests else None,
                deny_test_patterns=deny_tests,
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
                    jump_target = stage.context.get("jump_review_ref", stage.context.get("jump_quality_ref", "quality"))
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
                    outer_attempt = stage.context.get("_transient_retry_count", 0) + 1
                    stage.context["_transient_retry_count"] = outer_attempt
                    if e.is_auth_error or e.is_network_error:
                        retry_after = _OUTER_BACKOFF_CONNECTION.for_attempt(outer_attempt)
                    else:
                        retry_after = max(e.retry_after, _OUTER_BACKOFF_DEFAULT.for_attempt(outer_attempt))
                    emit(
                        M.RFAL,
                        f"Repair LLM transient failure, retrying in {retry_after:.0f}s: {e}",
                    )
                    raise TransientError(
                        f"LLM failed during repair: {e}",
                        retry_after=retry_after,
                        context_update={
                            "previous_failures": previous_failures,
                        },
                    )
                emit(M.RFAL, f"Repair LLM failed permanently: {e}")
                return TaskResult.failed_continue(error=f"Repair LLM failed permanently: {e}")

            except (OSError, RuntimeError, ValueError, KeyError) as e:  # repair: non-LLM execution errors
                emit(M.RFAL, f"RepairTask failed: {e}")
                logger.exception("RepairTask failed")
                return TaskResult.failed_continue(error=f"Repair failed: {e}")

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
            if result.returncode != 0 and "unrecognized arguments: --timeout" in (result.stderr or ""):
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
        except (subprocess.SubprocessError, OSError) as e:  # quick test: subprocess errors
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
        except OSError:  # prompt file read error
            logger.debug("Failed to read repairer prompt file", exc_info=True)
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


def _build_cross_module_hint(test_output: str, owned_files: list[str]) -> str:
    """Return a prompt hint when test errors suggest cross-module interface mismatches.

    Per-module repair agents can only modify their own files but often fail
    because their interface (constructor args, method names, return types) doesn't
    match what tests or other modules expect.  This hint tells the agent to READ
    the calling code (tests, other modules) and adapt its own interface.

    Returns an empty string when no cross-module patterns are detected.
    """
    if not detect_cross_module_failure(test_output):
        return ""

    owned_list = ", ".join(owned_files)
    return (
        "## Cross-Module Interface Mismatch Detected\n\n"
        "The test errors suggest that YOUR module's interface (constructor parameters, "
        "method signatures, exported names) does not match what the tests or other "
        "modules expect.\n\n"
        "**You MUST do the following before making changes:**\n"
        "1. READ the failing test files to understand the EXPECTED interface "
        "(constructor args, method names, return types).\n"
        "2. READ any other source files that import from your module to see how "
        "they call your code.\n"
        "3. ADAPT your implementation to match the expected interface — the tests "
        "are the specification, not your current code.\n\n"
        f"Your files: {owned_list}\n\n"
    )


# Use canonical implementations from validate_helpers to avoid duplication.
from ..tasks.validate_helpers import _build_test_env as _build_test_env  # noqa: F401, E402
from ..tasks.validate_helpers import detect_cross_module_failure  # noqa: E402
