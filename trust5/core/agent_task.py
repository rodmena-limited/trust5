import logging
import os
import threading
import time
from datetime import timedelta
from typing import Any

import yaml
from resilient_circuit import ExponentialDelay
from stabilize import StageExecution, Task, TaskResult
from stabilize.errors import TransientError

from ..core.agent import Agent
from ..core.context_builder import build_project_context
from ..core.ears import all_templates
from ..core.lang import LanguageProfile, build_language_context
from ..core.llm import LLM, LLMError
from ..core.mcp_manager import mcp_clients
from ..core.message import M, emit

logger = logging.getLogger(__name__)

# Outer (Stabilize-level) retry backoff for LLM errors.
# Inner retry in LLM._chat_with_retry already spent its budget;
# these delays let external conditions change before another attempt.
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

NON_INTERACTIVE_PREFIX = (
    "CRITICAL: You are running inside a fully autonomous, non-interactive pipeline. "
    "There is NO human at the terminal. You MUST make all decisions autonomously "
    "using sensible defaults. NEVER attempt to ask the user questions — the "
    "AskUserQuestion tool is NOT available. If you need to make a choice, pick the "
    "most reasonable option and proceed.\n\n"
    "IMPORTANT: /testbed does NOT exist. NEVER read, write, or cd to /testbed. "
    "All file paths must be relative to the current working directory.\n\n"
)

PLANNER_TOOLS = ["Read", "ReadFiles", "Glob", "Grep"]
TEST_WRITER_TOOLS = ["Read", "ReadFiles", "Write", "Edit", "Glob", "Grep"]


_STAGE_OUTPUT_KEYS = ("plan_output", "test_writer_output", "implementer_output")


def _resolve_allowed_tools(agent_name: str | None) -> list[str] | None:
    name_lower = (agent_name or "").lower()
    if "planner" in name_lower:
        return PLANNER_TOOLS
    if "test-writer" in name_lower or "test_writer" in name_lower:
        return TEST_WRITER_TOOLS
    return None


def _output_key_for_agent(agent_name: str | None) -> str:
    name_lower = (agent_name or "").lower()
    if "planner" in name_lower:
        return "plan_output"
    if "test-writer" in name_lower or "test_writer" in name_lower:
        return "test_writer_output"
    if "implementer" in name_lower:
        return "implementer_output"
    return "agent_output"


def _collect_ancestor_outputs(context: dict[str, Any]) -> list[str]:
    key_labels = {
        "plan_output": "Plan (from Planner)",
        "test_writer_output": "Test Specification (from Test Writer)",
        "implementer_output": "Implementation (from Implementer)",
        "agent_output": "Previous Stage Output",
    }
    sections: list[str] = []
    seen_keys: set[str] = set()
    for key in (*_STAGE_OUTPUT_KEYS, "agent_output"):
        value = context.get(key, "")
        if value:
            label = key_labels.get(key, key)
            sections.append(f"## {label}\n\n{value}")
            seen_keys.add(key)
    # Also check the nested ancestor_outputs dict (used by parallel pipeline
    # and strip_plan_stage) for any keys not already found via flat lookup.
    ancestor_dict = context.get("ancestor_outputs", {})
    if isinstance(ancestor_dict, dict):
        for nested_key, nested_value in ancestor_dict.items():
            # Map nested keys to flat keys: "plan" -> "plan_output"
            flat_key = f"{nested_key}_output" if not nested_key.endswith("_output") else nested_key
            if flat_key not in seen_keys and nested_value:
                label = key_labels.get(flat_key, flat_key)
                sections.append(f"## {label}\n\n{nested_value}")
    return sections


# Minimum content length (bytes) for a file to be considered "implemented".
# Stubs from _scaffold_module_files are ~50 chars; real implementations
# are typically 200+ chars.  The threshold must be low enough to catch
# empty/stub files but high enough to avoid false positives on legitimate
# small files (e.g. __init__.py re-exports, constants modules).
_STUB_THRESHOLD = 100


def _detect_stub_files(owned_files: list[str], project_root: str) -> list[str]:
    """Return owned files that are still stubs (missing, empty, or scaffold-only).
    - It doesn't exist on disk, OR
    - Its content is shorter than _STUB_THRESHOLD chars AND contains
      "implementation required" (the scaffold marker), OR
    - Its content is shorter than _STUB_THRESHOLD chars AND consists
      only of a module docstring (no actual code)
    __init__.py files are exempt from stub detection because they are
    legitimately small (re-exports, package markers).
    """
    stubs: list[str] = []
    for rel_path in owned_files:
        # Skip __init__.py files — they are legitimately small
        if os.path.basename(rel_path) == "__init__.py":
            continue
        full = os.path.join(project_root, rel_path)
        if not os.path.exists(full):
            stubs.append(rel_path)
            continue
        try:
            with open(full, encoding="utf-8") as fh:
                content = fh.read().strip()
        except (OSError, UnicodeDecodeError):
            continue

        if len(content) < _STUB_THRESHOLD:
            lower = content.lower()
            if "implementation required" in lower:
                stubs.append(rel_path)
            elif not content or (content.startswith('"""') and content.endswith('"""')):
                stubs.append(rel_path)
    return stubs


_TDD_GREEN_PHASE_INSTRUCTIONS = (
    "## TDD GREEN PHASE (auto-injected)\n\n"
    "Test files already exist from the RED phase. Your job is to:\n"
    "1. Read ALL existing test files first (use Glob to find *test* and *spec* files)\n"
    "2. Write ONLY source/implementation code to make the tests pass\n"
    "3. Do NOT create new test files — they already exist from the RED phase\n"
    "4. Do NOT modify existing test files — the tests define the specification\n"
    "5. Run tests after implementation to verify all existing tests pass\n"
    "6. If a test fails, fix the implementation — NEVER fix the test\n"
)


class AgentTask(Task):
    """Stabilize task that runs an LLM agent with configurable prompt and tools.

    Reads stage context for agent_name, prompt_file, user_input, model_tier,
    max_turns, and non_interactive flag. Supports rebuild signals from the
    watchdog and handles LLM/connection errors with resilient-circuit backoff.
    """

    def execute(self, stage: StageExecution) -> TaskResult:
        """Execute the agent task within the Stabilize workflow."""
        start_time = time.monotonic()
        agent_name: str | None = stage.context.get("agent_name")
        prompt_file: str | None = stage.context.get("prompt_file")
        user_input: str = stage.context.get("user_input", "")
        model_tier: str = stage.context.get("model_tier", "default")
        max_turns: int = int(stage.context.get("max_turns", 20))
        non_interactive: bool = bool(stage.context.get("non_interactive", False))

        def _emit_elapsed() -> None:
            elapsed = time.monotonic() - start_time
            emit(M.SELP, f"{elapsed:.1f}s")

        ancestor_sections = _collect_ancestor_outputs(stage.context)
        if ancestor_sections:
            combined = "\n\n".join(ancestor_sections)
            user_input = f"{combined}\n\n## Original User Request\n\n{user_input}"
            logger.debug(
                "Merged %d ancestor section(s) (%d chars) for %s",
                len(ancestor_sections),
                len(combined),
                agent_name,
            )

        # Inject acceptance criteria prominently for implementer and test-writer agents
        plan_config = stage.context.get("plan_config", {})
        acceptance_criteria = plan_config.get("acceptance_criteria", [])
        if acceptance_criteria and agent_name:
            name_lower = agent_name.lower()
            if any(k in name_lower for k in ("implementer", "test-writer", "test_writer")):
                numbered = "\n".join(f"  AC-{i + 1}. {c}" for i, c in enumerate(acceptance_criteria))
                criteria_header = (
                    "## MANDATORY ACCEPTANCE CRITERIA (from SPEC)\n\n"
                    "You MUST address ALL of the following criteria. "
                    "Missing criteria = pipeline failure.\n\n"
                    f"{numbered}\n\n"
                )
                if "implementer" in name_lower:
                    criteria_header += (
                        "Use the EXACT class/function names from the criteria above. "
                        "Do NOT rename or substitute them.\n\n"
                    )
                elif "test" in name_lower:
                    criteria_header += (
                        "Write at least one test per criterion. Name tests test_ac{N}_description for traceability.\n\n"
                    )
                user_input = criteria_header + user_input

        owned_files = stage.context.get("owned_files")
        test_files = stage.context.get("test_files")
        module_name = stage.context.get("module_name")

        # TDD enforcement: test_writer can write test files,
        # but implementer/repairer MUST NOT modify them.
        is_test_writer = agent_name and ("test-writer" in agent_name.lower() or "test_writer" in agent_name.lower())
        is_implementer_or_repairer = agent_name and (
            "implementer" in agent_name.lower() or "repairer" in agent_name.lower()
        )

        effective_owned: list[str] = []
        denied_for_agent: list[str] = []
        deny_test_patterns = False

        if is_test_writer:
            # Test writer: can ONLY write test files, never source files.
            # Source files are denied to prevent the LLM from creating
            # implementation stubs (a common TDD anti-pattern).
            if test_files:
                effective_owned.extend(test_files)
            if owned_files:
                denied_for_agent.extend(owned_files)
        elif is_implementer_or_repairer:
            # Implementer/repairer: source files only, test files are DENIED
            if owned_files:
                effective_owned.extend(owned_files)
            if test_files:
                denied_for_agent.extend(test_files)
            deny_test_patterns = True
        else:
            # Other agents (planner, etc.): combine as before
            if owned_files:
                effective_owned.extend(owned_files)
            if test_files:
                effective_owned.extend(test_files)

        if effective_owned or denied_for_agent:
            header = f" ({module_name})" if module_name else ""
            ownership_lines: list[str] = []
            if effective_owned:
                files_list = "\n".join(f"- {f}" for f in effective_owned)
                ownership_lines.append(
                    f"## Your Module Files{header}\n\n"
                    f"You MUST create/modify ONLY these files:\n{files_list}\n\n"
                    f"**CRITICAL — Parallel Module Rules:**\n"
                    f"- CREATE each of your files from scratch with your COMPLETE implementation.\n"
                    f"- Your files may already exist as placeholder stubs — OVERWRITE them with your full code.\n"
                    f"- NEVER try to modify, extract from, or move code from files owned by other modules.\n"
                    f"- Even if you see related code in other modules, write YOUR implementation independently.\n"
                    f"- If you need functionality from another module, import it — do not duplicate it.\n\n"
                )
            if denied_for_agent:
                denied_list = "\n".join(f"- {f}" for f in denied_for_agent)
                if is_test_writer:
                    # Test-writer is denied SOURCE files (owned_files)
                    denied_label = f"## READ-ONLY Source Files{header}\n\n"
                    denied_desc = (
                        "These source files are READ-ONLY. Do NOT modify or create them.\n"
                        f"{denied_list}\n\n"
                        "Your job is to write TESTS only. The source code will be written later "
                        "by the implementer agent based on your tests.\n\n"
                    )
                else:
                    # Implementer/repairer is denied TEST files
                    denied_label = f"## READ-ONLY Test Files{header}\n\n"
                    denied_desc = (
                        "These test files are READ-ONLY. Do NOT modify or delete them:\n"
                        f"{denied_list}\n\n"
                        "Tests define the specification. Fix the implementation, NEVER the tests.\n\n"
                    )
                ownership_lines.append(denied_label + denied_desc)
            user_input = "".join(ownership_lines) + user_input
            logger.debug(
                "Module '%s': %d owned, %d denied file(s)",
                module_name,
                len(effective_owned),
                len(denied_for_agent),
            )

        # Cross-module test visibility: tell implementers/repairers about
        # OTHER modules' test files so they can read them and match the
        # expected interface (constructor args, column names, method names).
        cross_module_tests = stage.context.get("cross_module_tests")
        if cross_module_tests and is_implementer_or_repairer:
            cm_lines = [
                "## Cross-Module Interface Reference\n\n"
                "Other modules' tests may import from YOUR code. "
                "Before implementing, READ these test files (especially their "
                "import statements and setup code) to understand what interface "
                "they expect — constructor parameters, method names, database "
                "column names, return types:\n\n"
            ]
            for cm_mod, cm_files in cross_module_tests.items():
                for tf in cm_files:
                    cm_lines.append(f"- {tf} ({cm_mod} module)\n")
            cm_lines.append(
                "\n**CRITICAL**: Your implementation must be compatible with "
                "ALL tests across all modules, not just your own module's "
                "tests. Mismatched interfaces are the #1 cause of pipeline "
                "failure in parallel builds.\n\n"
            )
            user_input = "".join(cm_lines) + user_input

        if not agent_name or not prompt_file:
            return TaskResult.terminal(error="AgentTask requires 'agent_name' and 'prompt_file' in context")

        mod_tag = f" ({module_name})" if module_name else ""
        emit(M.WSTG, f"AgentTask executing: {agent_name}{mod_tag}", label=module_name or "")

        system_prompt = self._load_system_prompt(prompt_file)
        if system_prompt is None:
            return TaskResult.terminal(error=f"Prompt file not found: {prompt_file}")

        if non_interactive:
            system_prompt = NON_INTERACTIVE_PREFIX + system_prompt

        project_root = os.getcwd()
        project_context = build_project_context(project_root)
        if project_context:
            system_prompt += "\n\n" + project_context

        profile_data = stage.context.get("language_profile")
        if profile_data:
            try:
                profile = LanguageProfile(**profile_data)
                system_prompt += "\n\n" + build_language_context(profile)
            except (TypeError, KeyError):
                pass

        if agent_name and "planner" in agent_name.lower():
            system_prompt += "\n\n" + _build_ears_context()

        if agent_name and "implementer" in agent_name.lower() and stage.context.get("test_first_completed"):
            system_prompt += "\n\n" + _TDD_GREEN_PHASE_INSTRUCTIONS

        from ..tasks.watchdog_task import load_watchdog_findings

        watchdog_ctx = load_watchdog_findings(project_root)
        if watchdog_ctx:
            system_prompt += "\n\n" + watchdog_ctx

        reimpl_count = stage.context.get("reimplementation_count", 0)
        if agent_name and "implementer" in agent_name.lower() and reimpl_count > 0:
            failure_summary = stage.context.get("failure_summary", "")
            reimpl_instructions = (
                f"## RE-IMPLEMENTATION ATTEMPT {reimpl_count} (auto-injected)\n\n"
                f"Previous implementation FAILED after multiple repair attempts. "
                f"You MUST take a COMPLETELY DIFFERENT approach this time.\n\n"
                f"DO NOT repeat the same implementation strategy. Rewrite from "
                f"scratch with a different design.\n\n"
                f"### What failed previously:\n\n{failure_summary}\n"
            )
            user_input = reimpl_instructions + "\n\n" + user_input

        cwd_prefix = (
            f"WORKING DIRECTORY: {project_root}\n"
            f"All files MUST be created relative to this directory. "
            f"Use '{project_root}/' as prefix or use relative paths. "
            f"/testbed does NOT exist.\n\n"
        )
        user_input = cwd_prefix + str(user_input)

        allowed_tools = _resolve_allowed_tools(agent_name)

        llm = LLM.for_tier(model_tier, stage_name=agent_name)

        with mcp_clients() as mcp:
            agent = Agent(
                name=agent_name,
                prompt=system_prompt,
                llm=llm,
                non_interactive=non_interactive,
                allowed_tools=allowed_tools,
                owned_files=effective_owned or None,
                denied_files=denied_for_agent or None,
                deny_test_patterns=deny_test_patterns,
                mcp_clients=mcp,
            )

            _emit_elapsed()
            # Start periodic elapsed time updates (every 5 seconds)
            _elapsed_stop_event = threading.Event()

            def _periodic_elapsed() -> None:
                while not _elapsed_stop_event.is_set():
                    _emit_elapsed()
                    _elapsed_stop_event.wait(5.0)

            _elapsed_thread = threading.Thread(target=_periodic_elapsed, daemon=True)
            _elapsed_thread.start()

            agent_timeout: float = float(stage.context.get("agent_timeout_seconds", 1800))
            try:
                result = agent.run(user_input, max_turns=max_turns, timeout_seconds=agent_timeout)
                output_key = _output_key_for_agent(agent_name)

                # For critical stages (planner), an empty response is not a
                # success — raise TransientError so Stabilize retries the task.
                if not result or not result.strip():
                    is_planner = agent_name and "planner" in agent_name.lower()
                    if is_planner:
                        emit(
                            M.SWRN,
                            f"[{agent_name}] Planner returned empty output — "
                            f"raising TransientError for Stabilize retry",
                        )
                        raise TransientError(
                            f"Planner {agent_name} returned empty output",
                            retry_after=30.0,
                        )

                # Post-execution verification for implementer agents:
                # Check that owned source files were actually written.
                # Without this, the LLM can exhaust its turns without writing
                # any code, return "success", and waste 10+ validate/repair
                # cycles on files that were never implemented.
                if is_implementer_or_repairer and effective_owned:
                    still_stubs = _detect_stub_files(effective_owned, project_root)
                    if still_stubs:
                        stub_list = ", ".join(still_stubs)
                        emit(
                            M.SWRN,
                            f"[{agent_name}] Agent finished but {len(still_stubs)} "
                            f"source file(s) are still stubs: {stub_list}",
                        )
                        return TaskResult.failed_continue(
                            error=(
                                f"Agent {agent_name} did not write implementation code. "
                                f"Files still empty/stubs: {still_stubs}"
                            ),
                            outputs={
                                "result": result,
                                output_key: result,
                                "stub_files": still_stubs,
                                "implementation_missing": True,
                            },
                        )

                return TaskResult.success(outputs={"result": result, output_key: result})
            except LLMError as e:
                emit(M.AERR, f"[{agent_name}] LLM error (retryable={e.retryable}, class={e.error_class}): {e}")
                if e.is_auth_error:
                    raise TransientError(
                        f"Auth failed for {agent_name}: {e}",
                        retry_after=120.0,
                    )
                if e.retryable or e.is_network_error:
                    # Inner retry already spent its budget (5 min for connection,
                    # 3 min for server). Pick a Stabilize-level wait that lets
                    # external conditions change before we burn another budget.
                    # Progressive: increase wait between outer retries.
                    outer_attempt = stage.context.get("_transient_retry_count", 0) + 1
                    stage.context["_transient_retry_count"] = outer_attempt
                    retry_after: float
                    if e.error_class == "connection":
                        retry_after = _OUTER_BACKOFF_CONNECTION.for_attempt(outer_attempt)
                    elif e.error_class == "rate_limit":
                        retry_after = max(e.retry_after, _OUTER_BACKOFF_DEFAULT.for_attempt(outer_attempt))
                    else:
                        retry_after = _OUTER_BACKOFF_DEFAULT.for_attempt(outer_attempt)
                    raise TransientError(
                        f"LLM failed for {agent_name}: {e}",
                        retry_after=retry_after,
                    )
                return TaskResult.failed_continue(error=f"LLM failed: {e}")
            except (OSError, RuntimeError, ValueError, KeyError) as e:  # agent: non-LLM execution errors
                emit(M.SERR, f"[{agent_name}] Agent execution failed: {e}")
                logger.exception("Agent %s failed", agent_name)
                return TaskResult.failed_continue(error=f"Agent execution failed: {e}")
            finally:
                _elapsed_stop_event.set()
                _elapsed_thread.join(timeout=2.0)
                _emit_elapsed()

    def _load_system_prompt(self, prompt_file: str) -> str | None:
        base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        assets_path = os.path.join(base_path, "assets")

        potential_paths = [
            os.path.join(assets_path, "prompts", prompt_file),
            os.path.join(assets_path, "claude", "agents", "moai", prompt_file),
        ]

        agent_def_path = None
        for p in potential_paths:
            if os.path.exists(p):
                agent_def_path = p
                break

        if not agent_def_path:
            return None

        try:
            with open(agent_def_path, encoding="utf-8") as f:
                content = f.read()
        except OSError as e:  # prompt file read error
            logger.error("Error reading prompt file %s: %s", prompt_file, e)
            return None

        frontmatter, body = _parse_frontmatter(content)
        system_prompt = body

        skills_list = _parse_skills_list(frontmatter.get("skills", ""))
        if skills_list:
            skill_content = _load_skills(skills_list, assets_path)
            if skill_content:
                system_prompt += "\n\n" + skill_content

        return system_prompt


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if content.startswith("---\n"):
        parts = content.split("---\n", 2)
        if len(parts) >= 3:
            try:
                fm = yaml.safe_load(parts[1])
                if isinstance(fm, dict):
                    return fm, parts[2]
            except yaml.YAMLError:
                pass
    return {}, content


def _parse_skills_list(skills_field: str | list[str] | Any) -> list[str]:
    if isinstance(skills_field, list):
        return skills_field
    if isinstance(skills_field, str):
        return [s.strip() for s in skills_field.split(",") if s.strip()]
    return []


def _load_skills(skills: list[str], assets_path: str) -> str:
    loaded = []
    for skill_name in skills:
        skill_path = os.path.join(assets_path, "claude", "skills", skill_name, "SKILL.md")
        if not os.path.exists(skill_path):
            skill_path = os.path.join(assets_path, "prompts", f"{skill_name}.md")

        if os.path.exists(skill_path):
            try:
                with open(skill_path, encoding="utf-8") as f:
                    content = f.read()
                _, body = _parse_frontmatter(content)
                loaded.append(f"--- SKILL: {skill_name} ---\n{body}\n")
            except OSError:  # skill file read error
                logger.debug("Failed to load skill %s", skill_name, exc_info=True)
    return "\n".join(loaded)


def _build_ears_context() -> str:
    """Build EARS template reference from core/ears.py for the planner."""
    lines = ["## EARS Requirement Templates (auto-injected)\n"]
    for tmpl in all_templates():
        lines.append(f"- **{tmpl.name}**: `{tmpl.template}` — {tmpl.description}")
    lines.append(
        "\nUse these patterns for ALL acceptance criteria. Tag each with [UBIQ], [EVENT], [STATE], [UNWNT], or [OPTNL]."
    )
    return "\n".join(lines)
