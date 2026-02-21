"""Parallel DAG builder for multi-module development pipelines."""

from __future__ import annotations

import os

from stabilize import StageExecution, TaskExecution, Workflow

from ..core.config import ConfigManager
from ..core.lang import detect_language, get_profile
from ..tasks.validate_helpers import _SOURCE_EXTENSIONS
from .module_spec import (
    ModuleSpec,
    _detect_dependency_cycle,
    _validate_file_ownership,
    _validate_module_completeness,
    extract_plan_output,
    parse_modules,
)
from .pipeline import MAX_REPAIR_JUMPS, _load_code_review_enabled, _load_mutation_enabled

# Re-export so existing ``from trust5.workflows.parallel_pipeline import ...`` keeps working.
__all__ = [
    "ModuleSpec",
    "create_parallel_develop_workflow",
    "extract_plan_output",
    "parse_modules",
]


# Primary extension per language — used to normalize extensionless paths from the planner.
_LANG_EXT: dict[str, str] = {
    "python": ".py",
    "go": ".go",
    "typescript": ".ts",
    "javascript": ".js",
    "rust": ".rs",
    "java": ".java",
    "ruby": ".rb",
}


def _normalize_module_paths(modules: list[ModuleSpec], profile_dict: dict[str, object]) -> None:
    """Add file extensions to module paths the planner emitted without them.

    The planner often outputs ``tests/test_task`` instead of ``tests/test_task.py``.
    Without normalization the test-writer agent interprets the bare path as a
    directory and creates an empty folder — resulting in 0 tests collected and
    an un-fixable validate/repair loop.

    Mutates *modules* in-place.
    """
    lang = str(profile_dict.get("language", "unknown"))
    default_ext = _LANG_EXT.get(lang, "")
    if not default_ext:
        return

    for mod in modules:
        mod.files = [_ensure_ext(f, default_ext) for f in (mod.files or [])]
        mod.test_files = [_ensure_ext(f, default_ext) for f in (mod.test_files or [])]


def _ensure_ext(path: str, default_ext: str) -> str:
    """Append *default_ext* to *path* if it lacks a recognized source extension."""
    _, ext = os.path.splitext(path)
    if ext.lower() in _SOURCE_EXTENSIONS:
        return path
    return path + default_ext


def _load_development_mode(project_root: str) -> str:
    try:
        mgr = ConfigManager(project_root)
        cfg = mgr.load_config()
        return cfg.quality.development_mode
    except Exception:
        return "hybrid"


def create_parallel_develop_workflow(
    modules: list[ModuleSpec],
    user_request: str,
    plan_output: str,
    setup_commands: list[str] | None = None,
    plan_config_dict: dict[str, object] | None = None,
) -> Workflow:
    # Validate before building stages — fail fast on broken module specs
    _validate_file_ownership(modules)
    _detect_dependency_cycle(modules)
    _validate_module_completeness(modules)

    project_root = os.getcwd()
    language = detect_language(project_root)
    profile = get_profile(language)
    profile_dict = profile.to_dict()

    # Normalize paths BEFORE building stages so the test-writer receives
    # proper file paths (e.g. "tests/test_task.py" not "tests/test_task").
    _normalize_module_paths(modules, profile_dict)

    dev_mode = _load_development_mode(project_root)
    use_tdd = dev_mode in ("tdd", "hybrid")
    use_mutation = _load_mutation_enabled(project_root)

    stages: list[StageExecution] = []

    setup = StageExecution(
        ref_id="setup",
        type="setup",
        name="Setup Environment",
        context={
            "project_root": project_root,
            "setup_commands": setup_commands or [],
        },
        requisite_stage_ref_ids=set(),
        tasks=[
            TaskExecution.create(
                name="Setup Environment",
                implementing_class="setup",
                stage_start=True,
                stage_end=True,
            )
        ],
    )
    stages.append(setup)

    validate_ref_ids: set[str] = set()

    # Give each module the full jump budget.  With FAILED_CONTINUE at the
    # jump limit (not TERMINAL), a stuck module no longer blocks other modules.
    # Protection comes from: per-module cap + TIMEOUT_DEVELOP + max_repair ×
    # max_reimplementations.  Integration and quality stages also get full budget.
    per_module_jumps = MAX_REPAIR_JUMPS

    for mod in modules:
        mid = mod.id
        # All modules start in parallel after setup.  Per-module tests are
        # scoped to owned_files, so no cross-module interference.  Cross-module
        # issues are caught later in integration_validate.
        dep_refs: set[str] = {"setup"}

        wt_ref = f"write_tests_{mid}"
        impl_ref = f"implement_{mid}"
        val_ref = f"validate_{mid}"
        rep_ref = f"repair_{mid}"

        module_context = {
            "jump_repair_ref": rep_ref,
            "jump_validate_ref": val_ref,
            "jump_implement_ref": impl_ref,
            "test_files": mod.test_files or None,
            "owned_files": mod.files or None,
            "module_name": mod.name,
        }

        if use_tdd:
            wt_ctx: dict[str, object] = {
                "agent_name": "test-writer",
                "prompt_file": "test-writer.md",
                "user_input": user_request,
                "model_tier": "good",
                "max_turns": 15,
                "non_interactive": True,
                "language_profile": profile_dict,
                "development_mode": dev_mode,
                "pipeline_phase": "run",
                "ancestor_outputs": {"plan": plan_output},
                **module_context,
            }
            if plan_config_dict:
                wt_ctx["plan_config"] = plan_config_dict

            write_tests = StageExecution(
                ref_id=wt_ref,
                type="agent",
                name=f"Write Tests ({mod.name})",
                context=wt_ctx,
                requisite_stage_ref_ids=dep_refs.copy(),
                tasks=[
                    TaskExecution.create(
                        name=f"Write Tests ({mod.name})",
                        implementing_class="agent",
                        stage_start=True,
                        stage_end=True,
                    )
                ],
            )
            stages.append(write_tests)

        impl_deps = {wt_ref} if use_tdd else dep_refs.copy()

        impl_ctx: dict[str, object] = {
            "agent_name": "implementer",
            "prompt_file": "implementer.md",
            "user_input": user_request,
            "model_tier": "best",
            "max_turns": 25,
            "non_interactive": True,
            "language_profile": profile_dict,
            "development_mode": dev_mode,
            "test_first_completed": use_tdd,
            "ancestor_outputs": {"plan": plan_output},
            **module_context,
        }
        if plan_config_dict:
            impl_ctx["plan_config"] = plan_config_dict

        implement = StageExecution(
            ref_id=impl_ref,
            type="agent",
            name=f"Implement ({mod.name})",
            context=impl_ctx,
            requisite_stage_ref_ids=impl_deps,
            tasks=[
                TaskExecution.create(
                    name=f"Implement ({mod.name})",
                    implementing_class="agent",
                    stage_start=True,
                    stage_end=True,
                )
            ],
        )
        stages.append(implement)

        validate_ctx: dict[str, object] = {
            "project_root": project_root,
            "max_repair_attempts": 5,
            "max_reimplementations": 3,
            "_max_jumps": per_module_jumps,
            "language_profile": profile_dict,
            "pipeline_phase": "run",
            "development_mode": dev_mode,
            "test_first_completed": use_tdd,
            **module_context,
        }
        if plan_config_dict:
            validate_ctx["plan_config"] = plan_config_dict

        validate = StageExecution(
            ref_id=val_ref,
            type="validate",
            name=f"Validate ({mod.name})",
            context=validate_ctx,
            requisite_stage_ref_ids={impl_ref},
            tasks=[
                TaskExecution.create(
                    name=f"Run Tests ({mod.name})",
                    implementing_class="validate",
                    stage_start=True,
                    stage_end=True,
                )
            ],
        )
        stages.append(validate)
        validate_ref_ids.add(val_ref)

        repair = StageExecution(
            ref_id=rep_ref,
            type="repair",
            name=f"Repair ({mod.name})",
            context={
                "project_root": project_root,
                "_max_jumps": per_module_jumps,
                "language_profile": profile_dict,
                "development_mode": dev_mode,
                **module_context,
            },
            requisite_stage_ref_ids={val_ref},
            tasks=[
                TaskExecution.create(
                    name=f"Fix Code ({mod.name})",
                    implementing_class="repair",
                    stage_start=True,
                    stage_end=True,
                )
            ],
        )
        stages.append(repair)

    # Aggregate all test files from all modules for integration stages
    all_test_files: list[str] = []
    for mod in modules:
        if mod.test_files:
            all_test_files.extend(mod.test_files)

    int_val_ctx: dict[str, object] = {
        "project_root": project_root,
        "max_repair_attempts": 5,
        "max_reimplementations": 2,
        "_max_jumps": MAX_REPAIR_JUMPS,
        "language_profile": profile_dict,
        "pipeline_phase": "run",
        "development_mode": dev_mode,
        "test_first_completed": use_tdd,
        "jump_repair_ref": "integration_repair",
        "jump_validate_ref": "integration_validate",
        "jump_implement_ref": "integration_repair",
        "test_files": all_test_files or None,
    }
    if plan_config_dict:
        int_val_ctx["plan_config"] = plan_config_dict

    int_val = StageExecution(
        ref_id="integration_validate",
        type="validate",
        name="Integration Validate (All Tests)",
        context=int_val_ctx,
        requisite_stage_ref_ids=validate_ref_ids,
        tasks=[
            TaskExecution.create(
                name="Run All Tests",
                implementing_class="validate",
                stage_start=True,
                stage_end=True,
            )
        ],
    )
    stages.append(int_val)

    int_rep = StageExecution(
        ref_id="integration_repair",
        type="repair",
        name="Integration Repair (Cross-Module Fix)",
        context={
            "project_root": project_root,
            "_max_jumps": MAX_REPAIR_JUMPS,
            "language_profile": profile_dict,
            "development_mode": dev_mode,
            "jump_repair_ref": "integration_repair",
            "jump_validate_ref": "integration_validate",
            "jump_quality_ref": "quality",
            "test_files": all_test_files or None,
        },
        requisite_stage_ref_ids={"integration_validate"},
        tasks=[
            TaskExecution.create(
                name="Fix Cross-Module Issues",
                implementing_class="repair",
                stage_start=True,
                stage_end=True,
            )
        ],
    )
    stages.append(int_rep)

    # Optional mutation testing stage (Oracle Problem mitigation)
    review_deps: set[str] = {"integration_repair"}
    if use_mutation:
        mutation = StageExecution(
            ref_id="mutation",
            type="mutation",
            name="Mutation Testing (Oracle Gate)",
            context={
                "project_root": project_root,
                "language_profile": profile_dict,
                "max_mutation_samples": 10,
            },
            requisite_stage_ref_ids={"integration_repair"},
            tasks=[
                TaskExecution.create(
                    name="Mutation Testing",
                    implementing_class="mutation",
                    stage_start=True,
                    stage_end=True,
                )
            ],
        )
        stages.append(mutation)
        review_deps = {"mutation"}

    # Optional LLM-based code review (single stage for all modules)
    use_review = _load_code_review_enabled(project_root)
    quality_deps: set[str] = review_deps.copy()
    if use_review:
        review_ctx: dict[str, object] = {
            "project_root": project_root,
            "language_profile": profile_dict,
            # Disable jump-to-repair in parallel pipelines (cross-module jump too complex)
            "code_review_jump_to_repair": False,
        }
        if plan_config_dict:
            review_ctx["plan_config"] = plan_config_dict
        review_ctx["ancestor_outputs"] = {"plan": plan_output}

        review = StageExecution(
            ref_id="review",
            type="review",
            name="Review (Code Analysis)",
            context=review_ctx,
            requisite_stage_ref_ids=review_deps,
            tasks=[
                TaskExecution.create(
                    name="Code Review",
                    implementing_class="review",
                    stage_start=True,
                    stage_end=True,
                )
            ],
        )
        stages.append(review)
        quality_deps = {"review"}

    quality_ctx: dict[str, object] = {
        "project_root": project_root,
        "quality_attempt": 0,
        "max_quality_attempts": 3,
        "_max_jumps": MAX_REPAIR_JUMPS,
        "language_profile": profile_dict,
        "pipeline_phase": "run",
        "development_mode": dev_mode,
        "test_first_completed": use_tdd,
        "jump_repair_ref": "integration_repair",
    }
    if plan_config_dict:
        quality_ctx["plan_config"] = plan_config_dict

    quality = StageExecution(
        ref_id="quality",
        type="quality",
        name="Quality (TRUST 5 Gate)",
        context=quality_ctx,
        requisite_stage_ref_ids=quality_deps,
        tasks=[
            TaskExecution.create(
                name="TRUST 5 Quality Gate",
                implementing_class="quality",
                stage_start=True,
                stage_end=True,
            )
        ],
    )
    stages.append(quality)

    return Workflow.create(
        application="trust5",
        name="Parallel Develop Pipeline",
        stages=stages,
    )
