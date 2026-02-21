import logging
import os
import subprocess
import time
from typing import Any

from stabilize import StageExecution, Task, TaskResult

from ..core.constants import (
    CONSECUTIVE_FAILURE_LIMIT,
    PYTEST_PER_TEST_TIMEOUT,
    TEST_OUTPUT_LIMIT,
)
from ..core.constants import MAX_REIMPLEMENTATIONS as _MAX_REIMPL_DEFAULT
from ..core.constants import MAX_REPAIR_ATTEMPTS as _MAX_REPAIR_DEFAULT
from ..core.context_keys import check_jump_limit, increment_jump_count, propagate_context
from ..core.lang import detect_language, get_profile
from ..core.message import M, emit, emit_block

# Import all helpers from the extracted module.
# Re-exported at module level so that existing imports (tests, repair_task)
# continue to work without changes.
from .validate_helpers import (
    _build_test_env,
    _count_tests,
    _derive_module_test_files,
    _discover_test_files,
    _filter_test_file_lint,
    _normalize_owned_files,
    _parse_command,
    _scope_lint_command,
    _scope_test_command,
    _strip_nonexistent_files,
)

logger = logging.getLogger(__name__)

MAX_REPAIR_ATTEMPTS = _MAX_REPAIR_DEFAULT
MAX_REIMPLEMENTATIONS = _MAX_REIMPL_DEFAULT

# Re-export helpers so "from trust5.tasks.validate_task import _parse_command" etc. still work.
__all__ = [
    "ValidateTask",
    "MAX_REPAIR_ATTEMPTS",
    "MAX_REIMPLEMENTATIONS",
    "_parse_command",
    "_filter_test_file_lint",
    "_normalize_owned_files",
    "_scope_lint_command",
    "_strip_nonexistent_files",
    "_scope_test_command",
    "_build_test_env",
    "_discover_test_files",
    "_derive_module_test_files",
    "_count_tests",
]


class ValidateTask(Task):
    """Runs syntax checks and tests, routing failures to repair via jump_to."""

    def execute(self, stage: StageExecution) -> TaskResult:
        start_time = time.monotonic()
        project_root = stage.context.get("project_root", os.getcwd())
        repair_attempt = stage.context.get("repair_attempt", 0)
        max_attempts = stage.context.get("max_repair_attempts", MAX_REPAIR_ATTEMPTS)
        profile_data = stage.context.get("language_profile", {})
        plan_config = stage.context.get("plan_config", {})

        # ── Normalize owned_files / test_files ────────────────────────
        # The planner often produces module paths without extensions
        # (e.g. "taskqueue/worker" instead of "taskqueue/worker.py").
        # Resolve them against the filesystem before any lint/test scoping.
        raw_owned = stage.context.get("owned_files")
        if raw_owned:
            stage.context["owned_files"] = _normalize_owned_files(raw_owned, project_root)
        raw_test_files = stage.context.get("test_files")
        if raw_test_files:
            stage.context["test_files"] = _normalize_owned_files(raw_test_files, project_root)

        # ── Jump-limit safety net ─────────────────────────────────────
        if check_jump_limit(stage.context):
            emit(
                M.VFAL,
                f"Jump limit reached "
                f"({stage.context.get('_jump_count', 0)}/{stage.context.get('_max_jumps', 50)}). "
                f"Marking as failed — pipeline continues with other modules.",
            )
            return TaskResult.failed_continue(
                error="Jump limit exceeded — validate/repair loop ran too long",
                outputs={"tests_passed": False, "jump_limit_reached": True},
            )

        # Resolve language profile: use context data, fall back to auto-detection.
        # When the build-time profile says "unknown" (pipeline was created before
        # setup ran and created manifest files), re-detect at runtime and update
        # the context so downstream stages (repair, reimplementation) also benefit.
        detected = detect_language(project_root)
        base_profile = get_profile(detected)
        if profile_data.get("language") == "unknown" and detected != "unknown":
            profile_data = base_profile.to_dict()
            stage.context["language_profile"] = profile_data
            logger.debug("Re-detected language as %s (was unknown)", detected)

        # Auto-install dev dependencies (ruff, pytest-timeout, etc.) if the
        # profile declares them and the project has a package manager available.
        if base_profile.dev_dependencies and base_profile.package_install_prefix:
            self._install_dev_deps(project_root, base_profile)

        # Auto-detect test files when not provided.
        # In parallel pipelines (owned_files is set), derive module-scoped
        # test files from owned source files to avoid cross-module interference.
        # In serial pipelines, discover all test files for the deny list.
        if not stage.context.get("test_files"):
            extensions = tuple(profile_data.get("extensions", base_profile.extensions))
            skip_dirs = tuple(profile_data.get("skip_dirs", base_profile.skip_dirs))
            discovered = _discover_test_files(project_root, extensions, skip_dirs)
            if discovered:
                owned_files = stage.context.get("owned_files")
                if owned_files:
                    # Parallel pipeline: scope to module's test files only
                    scoped = _derive_module_test_files(discovered, owned_files)
                    if scoped:
                        stage.context["test_files"] = scoped
                        logger.debug(
                            "Derived %d module-scoped test files from %d owned files",
                            len(scoped),
                            len(owned_files),
                        )
                    else:
                        logger.warning(
                            "Could not derive test files for module (owned=%s). Running without test scoping.",
                            owned_files,
                        )
                else:
                    # Serial pipeline: use all test files
                    stage.context["test_files"] = discovered
                    logger.debug("Auto-detected %d test files", len(discovered))

        def _emit_elapsed() -> None:
            elapsed = time.monotonic() - start_time
            emit(M.SELP, f"{elapsed:.1f}s")

        # Prefer planner-decided test command over profile default.
        # In parallel pipelines, scope test commands to module-specific test files
        # to prevent cross-module test interference.
        plan_test_cmd = plan_config.get("test_command") if plan_config else None
        if plan_test_cmd:
            test_cmd = _parse_command(plan_test_cmd)
        else:
            test_cmd = tuple(profile_data.get("test_command", base_profile.test_command))
        syntax_cmd_raw = profile_data.get("syntax_check_command")
        syntax_cmd = tuple(syntax_cmd_raw) if syntax_cmd_raw is not None else base_profile.syntax_check_command
        lang_name = profile_data.get("language", base_profile.language)
        module_name = stage.context.get("module_name", "")

        # Resolve module-scoped test files.  The planner may specify test_files
        # that don't actually exist (the test writer may create different names).
        # When that happens in parallel mode, auto-derive from actual test files.
        scoped_test_files = stage.context.get("test_files")
        owned_files_for_tests = stage.context.get("owned_files")
        if scoped_test_files:
            existing = [f for f in scoped_test_files if os.path.exists(os.path.join(project_root, f))]
            if existing:
                # Planner's test files exist — use them
                if plan_test_cmd and owned_files_for_tests:
                    # Parallel mode: scope test command to specific files
                    plan_test_cmd_scoped = _scope_test_command(plan_test_cmd, existing)
                    test_cmd = _parse_command(plan_test_cmd_scoped)
                else:
                    test_cmd = (*test_cmd, *existing)
            elif owned_files_for_tests:
                # Parallel mode: planner's test files don't exist.
                # Auto-derive from actually existing test files.
                extensions = tuple(profile_data.get("extensions", base_profile.extensions))
                skip_dirs = tuple(profile_data.get("skip_dirs", base_profile.skip_dirs))
                discovered = _discover_test_files(project_root, extensions, skip_dirs)
                derived = _derive_module_test_files(discovered, owned_files_for_tests)
                if derived:
                    logger.info(
                        "Planner test files missing (%s); auto-derived %d test file(s) for module",
                        scoped_test_files,
                        len(derived),
                    )
                    if plan_test_cmd:
                        plan_test_cmd_scoped = _scope_test_command(plan_test_cmd, derived)
                        test_cmd = _parse_command(plan_test_cmd_scoped)
                    else:
                        test_cmd = (*test_cmd, *derived)
                else:
                    logger.warning(
                        "No test files found for module (owned=%s, planned=%s). Skipping test run for this module.",
                        owned_files_for_tests,
                        scoped_test_files,
                    )
                    # Replace test command with a no-op so we don't run
                    # the global test suite for this module.
                    test_cmd = ("true",)

        # Inject per-test timeout for pytest to prevent a single blocking test
        # from consuming the entire subprocess timeout budget (120s).
        if lang_name == "python" and any("pytest" in str(t) for t in test_cmd):
            if not any("--timeout" in str(t) for t in test_cmd):
                test_cmd = (*test_cmd, f"--timeout={PYTEST_PER_TEST_TIMEOUT}")

        emit(
            M.VRUN,
            f"ValidateTask running [{lang_name}] (attempt {repair_attempt}/{max_attempts}) in {project_root}",
            label=module_name,
        )

        # Resolve lint-check commands: prefer planner-provided lint_command
        # (which may activate a venv), fall back to profile defaults.
        # Two layers of protection against stale file references:
        # 1. Parallel pipelines: scope to owned files only
        # 2. All pipelines: strip file tokens that don't exist on disk
        plan_lint_cmd = plan_config.get("lint_command") if plan_config else None
        # If the planner's lint command is just a syntax checker (defined per
        # language profile), skip it — _check_syntax() already covers it and
        # file-arg scoping causes spurious failures.
        if plan_lint_cmd and base_profile.syntax_check_tool_names:
            if any(t in plan_lint_cmd for t in base_profile.syntax_check_tool_names):
                logger.info("Ignoring syntax-check plan lint_command %r; using profile lint", plan_lint_cmd)
                plan_lint_cmd = None
        if plan_lint_cmd:
            owned = stage.context.get("owned_files")
            if owned:
                plan_lint_cmd = _scope_lint_command(plan_lint_cmd, owned)
            plan_lint_cmd = _strip_nonexistent_files(plan_lint_cmd, project_root, owned_files=owned)
            lint_cmds = [_parse_command(plan_lint_cmd)]
        else:
            lint_check_raw = profile_data.get("lint_check_commands", ())
            if not lint_check_raw:
                lint_check_raw = base_profile.lint_check_commands
            lint_cmds = [_parse_command(c) for c in lint_check_raw] if lint_check_raw else []

        # Detect source root layout and build env for subprocess calls.
        test_env = _build_test_env(project_root, profile_data)

        syntax_result = self._check_syntax(project_root, syntax_cmd, env=test_env)
        if syntax_result is not None:
            return self._handle_failure(
                stage,
                syntax_result,
                repair_attempt,
                max_attempts,
                "syntax",
                profile_data,
            )

        owned_files = stage.context.get("owned_files")
        lint_result = self._check_lint(project_root, lint_cmds, env=test_env, owned_files=owned_files)
        if lint_result is not None:
            return self._handle_failure(
                stage,
                lint_result,
                repair_attempt,
                max_attempts,
                "lint",
                profile_data,
            )

        test_result = self._run_tests(project_root, test_cmd, env=test_env)
        if test_result["passed"]:
            mod_label = f" ({module_name})" if module_name else ""
            emit(M.VPAS, f"All tests passed!{mod_label} ({test_result.get('total', 0)} tests)", label=module_name or "")
            return TaskResult.success(
                outputs={
                    "tests_passed": True,
                    "test_output": test_result["output"][:TEST_OUTPUT_LIMIT],
                    "total_tests": test_result.get("total", 0),
                    "repair_attempts_used": repair_attempt,
                }
            )

        return self._handle_failure(
            stage,
            test_result["output"],
            repair_attempt,
            max_attempts,
            "test",
            profile_data,
        )

    def _handle_failure(
        self,
        stage: StageExecution,
        output: str,
        attempt: int,
        max_attempts: int,
        failure_type: str,
        profile_data: dict[str, Any],
    ) -> TaskResult:
        previous = stage.context.get("previous_failures", [])
        summary = output[:500]
        updated_failures = previous + [summary]
        module_name = stage.context.get("module_name", "")
        mod_tag = f" [{module_name}]" if module_name else ""

        # Check reimplementation budget — shared by both escalation paths.
        reimpl_count = stage.context.get("reimplementation_count", 0)
        max_reimpl = stage.context.get("max_reimplementations", MAX_REIMPLEMENTATIONS)

        # Detect repeated identical failures (same error N times in a row).
        # Instead of giving up, trigger reimplementation — repair can't fix a
        # fundamentally broken implementation, but a fresh implement attempt can.
        n = CONSECUTIVE_FAILURE_LIMIT
        if len(updated_failures) >= n:
            recent = updated_failures[-n:]
            if all(f == recent[0] for f in recent):
                if reimpl_count < max_reimpl:
                    emit(
                        M.VFAL,
                        f"Same failure repeated {n} times{mod_tag}. "
                        f"Re-implementing from scratch "
                        f"(reimplementation {reimpl_count + 1}/{max_reimpl})",
                    )
                    return self._jump_to_reimplementation(
                        stage, output, updated_failures, reimpl_count,
                        max_attempts, profile_data,
                    )
                emit(
                    M.VFAL,
                    f"Same failure repeated {n} times{mod_tag}, "
                    f"all reimplementations exhausted. Continuing pipeline.",
                )
                return TaskResult.failed_continue(
                    error=f"Repeated failure ({n}x): {recent[0][:200]}",
                    outputs={
                        "tests_passed": False,
                        "failure_type": failure_type,
                        "repeated_failure": True,
                    },
                )

        if attempt >= max_attempts:
            if reimpl_count < max_reimpl:
                emit(
                    M.VFAL,
                    f"Repair exhausted ({max_attempts} attempts).{mod_tag} "
                    f"Re-implementing from scratch "
                    f"(reimplementation {reimpl_count + 1}/{max_reimpl})",
                )
                return self._jump_to_reimplementation(
                    stage, output, updated_failures, reimpl_count,
                    max_attempts, profile_data,
                )

            emit(
                M.VFAL,
                f"All reimplementation attempts exhausted{mod_tag} "
                f"({max_reimpl} reimplementations × {max_attempts} repairs). "
                f"Continuing pipeline.",
            )
            return TaskResult.failed_continue(
                error=(
                    f"Tests still failing after {max_reimpl} reimplementations "
                    f"× {max_attempts} repairs = "
                    f"{max_reimpl * max_attempts} total attempts"
                ),
                outputs={"tests_passed": False, "all_attempts_exhausted": True},
            )

        emit(
            M.VFAL,
            f"{failure_type} failure detected.{mod_tag} Jumping to repair (attempt {attempt + 1}/{max_attempts})",
        )
        emit_block(M.VTST, f"Failure output ({failure_type})", output[:2000], max_lines=40)

        repair_context: dict[str, Any] = {
            "_repair_requested": True,
            "test_output": output[:TEST_OUTPUT_LIMIT],
            "tests_passed": False,
            "tests_partial": False,
            "previous_failures": updated_failures[-5:],
            "failure_type": failure_type,
            "project_root": stage.context.get("project_root", os.getcwd()),
            "spec_id": stage.context.get("spec_id"),
            "language_profile": profile_data,
        }
        propagate_context(stage.context, repair_context)
        # Set repair_attempt AFTER propagate_context to prevent stale
        # stage.context["repair_attempt"] from overwriting the increment.
        repair_context["repair_attempt"] = attempt + 1
        increment_jump_count(repair_context)
        return TaskResult.jump_to(
            stage.context.get("jump_repair_ref", "repair"),
            context=repair_context,
        )

    def _jump_to_reimplementation(
        self,
        stage: StageExecution,
        output: str,
        updated_failures: list[str],
        reimpl_count: int,
        max_attempts: int,
        profile_data: dict[str, Any],
    ) -> TaskResult:
        """Build context and jump to the implement stage for a fresh attempt."""
        repair_history = updated_failures[-max_attempts:]
        reimpl_context: dict[str, Any] = {
            "previous_test_failures": output[:TEST_OUTPUT_LIMIT],
            "repair_history": repair_history,
            "failure_summary": (
                f"After {max_attempts} repair attempts, "
                f"these tests still fail:\n{output[:2000]}\n\n"
                f"Repair attempts tried:\n"
                + "\n---\n".join(f"Attempt {i + 1}: {f}" for i, f in enumerate(repair_history))
            ),
            "project_root": stage.context.get("project_root", os.getcwd()),
            "language_profile": profile_data,
        }
        propagate_context(stage.context, reimpl_context)
        # Reset counters — fresh implementation gets a clean slate.
        reimpl_context["repair_attempt"] = 0
        reimpl_context["reimplementation_count"] = reimpl_count + 1
        reimpl_context["previous_failures"] = []
        increment_jump_count(reimpl_context)
        return TaskResult.jump_to(
            stage.context.get("jump_implement_ref", "implement"),
            context=reimpl_context,
        )

    # Track which project roots have had dev deps installed to avoid
    # re-running pip on every validate cycle (class-level cache).
    _dev_deps_installed: set[str] = set()

    @classmethod
    def _install_dev_deps(cls, project_root: str, profile: object) -> None:
        """Install dev dependencies (ruff, pytest-timeout, etc.) if missing."""
        cache_key = project_root
        if cache_key in cls._dev_deps_installed:
            return
        cls._dev_deps_installed.add(cache_key)

        prefix = getattr(profile, "package_install_prefix", "")
        deps = getattr(profile, "dev_dependencies", ())
        if not prefix or not deps:
            return

        cmd_parts = prefix.split() + ["--quiet"] + list(deps)

        # Use venv-aware env so pip installs into the project venv
        env: dict[str, str] | None = None
        for venv_dir in (".venv", "venv"):
            venv_bin = os.path.join(project_root, venv_dir, "bin")
            if os.path.isdir(venv_bin):
                env = os.environ.copy()
                env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"
                env["VIRTUAL_ENV"] = os.path.join(project_root, venv_dir)
                env.pop("PYTHONHOME", None)
                break

        try:
            result = subprocess.run(
                cmd_parts,
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )
            if result.returncode == 0:
                logger.info("Installed dev dependencies: %s", ", ".join(deps))
            else:
                logger.warning("Failed to install dev deps: %s", result.stderr[:200])
        except Exception as exc:
            logger.warning("Dev dependency install error: %s", exc)

    @staticmethod
    def _check_syntax(
        project_root: str,
        syntax_cmd: tuple[str, ...] | None,
        env: dict[str, str] | None = None,
    ) -> str | None:
        """Run syntax check using the profile's syntax_check_command."""
        if syntax_cmd is None:
            return None

        try:
            result = subprocess.run(
                list(syntax_cmd),
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
            if result.returncode != 0:
                return f"Syntax check failed:\n{result.stdout}\n{result.stderr}"
        except subprocess.TimeoutExpired:
            return "Syntax check timed out after 120s"
        except FileNotFoundError:
            logger.warning("Syntax check tool not found: %s", syntax_cmd[0])
            return None
        except Exception as e:
            return f"Syntax check error: {e}"

        return None

    @staticmethod
    def _check_lint(
        project_root: str,
        lint_cmds: list[tuple[str, ...]],
        env: dict[str, str] | None = None,
        owned_files: list[str] | None = None,
    ) -> str | None:
        """Run lint-check commands; returns error output or None if all pass."""
        if not lint_cmds:
            return None

        errors: list[str] = []
        for cmd in lint_cmds:
            try:
                result = subprocess.run(
                    list(cmd),
                    cwd=project_root,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=env,
                )
                if result.returncode != 0:
                    output = (result.stdout + "\n" + result.stderr).strip()
                    # Treat "No module named X" as tool-not-installed, not lint error.
                    # This prevents false lint failures when e.g. ruff is not installed.
                    if "No module named" in output:
                        logger.warning("Lint module not installed: %s", " ".join(cmd[:3]))
                        continue
                    # Strip errors in test files and unowned files — the repairer
                    # cannot modify them, creating an unwinnable repair cycle.
                    output = _filter_test_file_lint(output, owned_files=owned_files)
                    if not output:
                        logger.info("All lint errors are in test files — treating as clean.")
                        continue
                    errors.append(f"Lint check failed ({' '.join(cmd[:2])}):\n{output}")
            except subprocess.TimeoutExpired:
                errors.append(f"Lint check timed out after 120s ({' '.join(cmd[:2])})")
            except FileNotFoundError:
                logger.warning("Lint tool not found: %s", cmd[0])
                continue
            except Exception as e:
                errors.append(f"Lint check error ({' '.join(cmd[:2])}): {e}")

        return "\n\n".join(errors) if errors else None

    @staticmethod
    def _run_tests(
        project_root: str,
        test_cmd: tuple[str, ...],
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            result = subprocess.run(
                list(test_cmd),
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
            # Self-heal: if --timeout flag isn't recognized (pytest-timeout
            # not installed in the target env), retry without it so we get
            # real test output instead of a useless argument error.
            if (
                result.returncode != 0
                and "unrecognized arguments: --timeout" in (result.stderr or "")
            ):
                cleaned = [t for t in test_cmd if not t.startswith("--timeout")]
                if len(cleaned) < len(list(test_cmd)):
                    logger.info("pytest-timeout not available, retrying without --timeout")
                    result = subprocess.run(
                        cleaned,
                        cwd=project_root,
                        capture_output=True,
                        text=True,
                        timeout=120,
                        env=env,
                    )
        except subprocess.TimeoutExpired:
            return {
                "passed": False,
                "output": f"Tests timed out after 120s (cmd: {' '.join(test_cmd)})",
                "total": 0,
            }
        except FileNotFoundError:
            return {
                "passed": False,
                "output": f"Test runner not found: {test_cmd[0]}",
                "total": 0,
            }
        except Exception as e:
            return {"passed": False, "output": f"Test execution error: {e}", "total": 0}

        output = result.stdout + "\n" + result.stderr
        passed = result.returncode == 0
        total = _count_tests(output)
        return {"passed": passed, "output": output, "total": total}
