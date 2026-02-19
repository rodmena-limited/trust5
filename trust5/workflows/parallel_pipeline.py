"""Parallel DAG builder for multi-module development pipelines."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

from stabilize import StageExecution, TaskExecution, Workflow

from ..core.config import ConfigManager
from ..core.lang import detect_language, get_profile
from .pipeline import MAX_REPAIR_JUMPS, _load_code_review_enabled, _load_mutation_enabled

logger = logging.getLogger(__name__)

_MODULES_RE = re.compile(r"<!--\s*MODULES\s*\n(.*?)\n\s*-->", re.DOTALL)


@dataclass
class ModuleSpec:
    id: str
    name: str
    files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    deps: list[str] = field(default_factory=list)


def parse_modules(workflow: Workflow) -> list[ModuleSpec]:
    raw = extract_plan_output(workflow)
    if not raw:
        return [ModuleSpec(id="main", name="Main")]

    match = _MODULES_RE.search(raw)
    if not match:
        return [ModuleSpec(id="main", name="Main")]

    try:
        data = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        logger.warning("Failed to parse MODULES JSON, falling back to serial")
        return [ModuleSpec(id="main", name="Main")]

    if not isinstance(data, list) or len(data) == 0:
        return [ModuleSpec(id="main", name="Main")]

    modules: list[ModuleSpec] = []
    for item in data:
        if not isinstance(item, dict) or "id" not in item:
            continue
        modules.append(
            ModuleSpec(
                id=str(item["id"]),
                name=str(item.get("name", item["id"])),
                files=item.get("files", []),
                test_files=item.get("test_files", []),
                deps=item.get("deps", []),
            )
        )

    return modules if modules else [ModuleSpec(id="main", name="Main")]


def extract_plan_output(workflow: Workflow) -> str:
    for stage in workflow.stages:
        if stage.ref_id == "plan":
            outputs = stage.outputs or {}
            return str(outputs.get("response", outputs.get("result", "")))
    return ""


def _load_development_mode(project_root: str) -> str:
    try:
        mgr = ConfigManager(project_root)
        cfg = mgr.load_config()
        return cfg.quality.development_mode
    except Exception:
        return "hybrid"


def _validate_file_ownership(modules: list[ModuleSpec]) -> None:
    """Raise if any file appears in more than one module's owned_files.

    Parallel execution with overlapping file ownership leads to concurrent
    writes that corrupt files.  Fail fast with a clear error message.
    """
    seen: dict[str, str] = {}  # file -> module_id
    conflicts: list[str] = []
    for mod in modules:
        for f in mod.files:
            if f in seen:
                conflicts.append(f"{f!r} claimed by both {seen[f]!r} and {mod.id!r}")
            else:
                seen[f] = mod.id
    if conflicts:
        raise ValueError(
            f"File ownership conflict in parallel pipeline — "
            f"{len(conflicts)} file(s) claimed by multiple modules:\n" + "\n".join(f"  - {c}" for c in conflicts)
        )


def _detect_dependency_cycle(modules: list[ModuleSpec]) -> None:
    """Raise if module dependencies form a cycle (would deadlock the DAG).

    Uses iterative DFS with a coloring scheme:
    WHITE=unvisited, GRAY=in-progress, BLACK=finished.
    """
    module_ids = {m.id for m in modules}
    deps_map = {m.id: [d for d in m.deps if d in module_ids] for m in modules}

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {mid: WHITE for mid in module_ids}

    for start in module_ids:
        if color[start] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        color[start] = GRAY
        while stack:
            node, idx = stack.pop()
            children = deps_map.get(node, [])
            if idx < len(children):
                stack.append((node, idx + 1))
                child = children[idx]
                if color[child] == GRAY:
                    raise ValueError(
                        f"Module dependency cycle detected involving {child!r}. "
                        f"Stabilize would deadlock. Fix the module dependency graph."
                    )
                if color[child] == WHITE:
                    color[child] = GRAY
                    stack.append((child, 0))
            else:
                color[node] = BLACK


_FACADE_FILES = frozenset({
    "__init__.py", "base.py", "index.py", "index.ts", "index.js",
    "mod.rs", "lib.rs", "main.go",
})


def _validate_module_completeness(modules: list[ModuleSpec]) -> None:
    """Warn about modules whose sole source file is likely a facade."""
    for mod in modules:
        if len(mod.files) == 1:
            basename = os.path.basename(mod.files[0])
            if basename in _FACADE_FILES:
                logger.warning(
                    "Module '%s' has only one file (%s) which is typically a facade/re-export. "
                    "This may indicate the module is missing its actual implementation files.",
                    mod.name,
                    mod.files[0],
                )


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

    module_ids = {m.id for m in modules}
    validate_ref_ids: set[str] = set()

    for mod in modules:
        mid = mod.id
        dep_refs: set[str] = {"setup"}
        for dep in mod.deps:
            if dep in module_ids:
                dep_refs.add(f"validate_{dep}")

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
            "_max_jumps": MAX_REPAIR_JUMPS,
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
                "_max_jumps": MAX_REPAIR_JUMPS,
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
