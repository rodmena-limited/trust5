"""Parallel DAG builder for multi-module development pipelines."""

from __future__ import annotations

import logging
import os

from stabilize import StageExecution, TaskExecution, Workflow

from ..core.config import ConfigManager, load_global_config  # noqa: F401
from ..core.lang import detect_language, get_profile
from .module_spec import (
    ModuleSpec,
    _detect_dependency_cycle,
    _validate_file_ownership,
    _validate_module_completeness,
    extract_plan_output,
    parse_modules,
)
from .pipeline import _load_code_review_enabled, _load_mutation_enabled, _load_pipeline_limits

# Re-export so existing ``from trust5.workflows.parallel_pipeline import ...`` keeps working.
__all__ = [
    "ModuleSpec",
    "create_parallel_develop_workflow",
    "extract_plan_output",
    "parse_modules",
]


logger = logging.getLogger(__name__)

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

# Minimal stub content per language.  The "{name}" placeholder is replaced with
# the file's stem (basename without extension).  Stubs are intentionally tiny —
# just enough for the agent to see the file exists and write its implementation there.
_STUB_TEMPLATES: dict[str, str] = {
    "python": '"""Module: {name} — implementation required."""\n',
    "go": "package {package}\n",
    "typescript": "// Module: {name} — implementation required.\nexport {{}};\n",
    "javascript": "// Module: {name} — implementation required.\nmodule.exports = {{}};\n",
    "rust": "// Module: {name} — implementation required.\n",
    "java": "// Module: {name} — implementation required.\n",
    "ruby": "# Module: {name} — implementation required.\n",
}
_DEFAULT_STUB = "// Implementation required.\n"

# Languages that require package marker files in every directory for imports to work.
_PACKAGE_MARKERS: dict[str, str] = {
    "python": "__init__.py",
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


# Non-source extensions that should never get a language extension appended.
# These are config, manifest, data, and tooling files that happen to appear in
# module.files because the planner includes them in the module's ownership.
_NON_SOURCE_EXTENSIONS = frozenset(
    (
        ".toml",
        ".cfg",
        ".ini",
        ".yaml",
        ".yml",
        ".json",
        ".xml",
        ".txt",
        ".md",
        ".rst",
        ".lock",
        ".mod",
        ".sum",
        ".gradle",
        ".properties",
        ".sbt",
        ".cabal",
        ".dockerfile",
        ".mk",
        ".cmake",
        ".gitignore",
        ".editorconfig",
        ".env",
        ".html",
        ".css",
        ".scss",
        ".less",
        ".svg",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".bat",
        ".ps1",
    )
)


def _ensure_ext(path: str, default_ext: str) -> str:
    """Append *default_ext* to *path* only when it has NO extension at all.
    (.py, .go) or a config/manifest extension (.toml, .cfg, .json) — are returned
    unchanged.  Only bare names like ``tests/test_task`` (no dot in the basename)
    get the language default appended.  Dotfiles like ``.gitignore`` are never
    modified.
    """
    basename = os.path.basename(path)
    _, ext = os.path.splitext(path)
    if not ext and not basename.startswith("."):
        return path + default_ext
    return path


def _scaffold_module_files(
    modules: list[ModuleSpec],
    project_root: str,
    language: str,
) -> None:
    """Pre-create minimal stub files for each module's owned source files.

    Parallel agents that start without a visible target file tend to write
    all functionality into a single facade file (e.g. ``__init__.py``), then
    other modules fail because they try to modify the facade instead of
    creating their own files.  Pre-creating stubs anchors each agent to its
    own files.

    Rules:
    - Only source files (``mod.files``) are stubbed — never ``mod.test_files``.
    - Existing files are never overwritten (safe for resume / incremental runs).
    - Parent directories are created as needed.
    - For Python, ``__init__.py`` package markers are created in every
      intermediate directory that doesn't already have one.
    """
    stub_template = _STUB_TEMPLATES.get(language, _DEFAULT_STUB)
    package_marker_name = _PACKAGE_MARKERS.get(language)
    marker_dirs_needed: set[str] = set()

    for mod in modules:
        for file_path in mod.files or []:
            full_path = os.path.join(project_root, file_path)

            # Create parent directories
            parent_dir = os.path.dirname(full_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)

            # Collect intermediate directories for package marker creation
            if package_marker_name:
                parts = file_path.replace("\\", "/").split("/")
                for i in range(1, len(parts)):
                    marker_dirs_needed.add("/".join(parts[:i]))

            # Create stub file only if it doesn't already exist
            if os.path.exists(full_path):
                continue

            basename = os.path.splitext(os.path.basename(file_path))[0]
            if language == "go":
                dir_name = os.path.basename(os.path.dirname(file_path)) or "main"
                content = stub_template.format(package=dir_name, name=basename)
            else:
                content = stub_template.format(name=basename)

            try:
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(content)
                logger.info("Scaffolded stub: %s", file_path)
            except OSError:
                logger.warning("Failed to scaffold stub: %s", file_path)

    # Create package marker files (__init__.py) in intermediate directories.
    if package_marker_name:
        for dir_path in sorted(marker_dirs_needed):
            marker_rel = f"{dir_path}/{package_marker_name}"
            marker_full = os.path.join(project_root, marker_rel)
            if not os.path.exists(marker_full):
                os.makedirs(os.path.dirname(marker_full) or project_root, exist_ok=True)
                try:
                    with open(marker_full, "w", encoding="utf-8") as f:
                        f.write("")
                    logger.info("Created package marker: %s", marker_rel)
                except OSError:
                    logger.warning("Failed to create package marker: %s", marker_rel)


# Reverse map: extension → language (inverted from _LANG_EXT).
_EXT_TO_LANG: dict[str, str] = {ext: lang for lang, ext in _LANG_EXT.items()}


def _infer_language_from_modules(modules: list[ModuleSpec]) -> str:
    """Infer the project language from module file extensions.

    When ``detect_language()`` returns ``"unknown"`` (no manifest files in a
    greenfield project), we fall back to checking what extensions the planner
    assigned to source files.  If all files share the same recognized extension,
    we use that language.
    """
    from collections import Counter

    ext_counts: Counter[str] = Counter()
    for mod in modules:
        for f in mod.files or []:
            _, ext = os.path.splitext(f)
            if ext:
                ext_counts[ext.lower()] += 1

    if not ext_counts:
        return "unknown"

    # Most common extension wins
    most_common_ext, _ = ext_counts.most_common(1)[0]
    return _EXT_TO_LANG.get(most_common_ext, "unknown")


def _load_development_mode(project_root: str) -> str:
    try:
        mgr = ConfigManager(project_root)
        cfg = mgr.load_config()
        return cfg.quality.development_mode
    except Exception:
        logger.debug("Failed to load development mode, defaulting to 'hybrid'", exc_info=True)
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
    limits = _load_pipeline_limits(project_root)
    language = detect_language(project_root)

    # Greenfield projects have no manifest files yet so detect_language
    # returns "unknown".  Fall back to inferring from module file extensions.
    if language == "unknown":
        language = _infer_language_from_modules(modules)

    profile = get_profile(language)
    profile_dict = profile.to_dict()

    # Normalize paths BEFORE building stages so the test-writer receives
    # proper file paths (e.g. "tests/test_task.py" not "tests/test_task").
    _normalize_module_paths(modules, profile_dict)

    # Pre-create stub files so parallel agents see their target files
    # and write implementations there instead of trying to modify other
    # modules' files (the "facade collision" anti-pattern).
    _scaffold_module_files(modules, project_root, language)

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

    watchdog = StageExecution(
        ref_id="watchdog",
        type="watchdog",
        name="Watchdog (Pipeline Monitor)",
        context={
            "project_root": project_root,
            "language_profile": profile_dict,
            "model_tier": "watchdog",
            "workflow_timeout": int(load_global_config().timeouts.develop),
        },
        requisite_stage_ref_ids=set(),
        tasks=[
            TaskExecution.create(
                name="Pipeline Health Monitor",
                implementing_class="watchdog",
                stage_start=True,
                stage_end=True,
            )
        ],
    )
    stages.append(watchdog)

    validate_ref_ids: set[str] = set()
    repair_ref_ids: set[str] = set()

    # Per-module budget is intentionally lower than the serial pipeline budget.
    # In parallel pipelines, cross-module interface mismatches are the primary
    # failure mode, and per-module repair *cannot* fix them (it can only modify
    # its own files).  A shorter budget lets integration_validate/repair kick
    # in sooner — those stages run WITHOUT owned_files and CAN fix cross-module
    # issues.  30 jumps still allows 2 full reimplementation cycles.
    per_module_jumps = limits["per_module_max_jumps"]

    # Collect all test files for cross-module interface visibility.
    # Each implementer will see OTHER modules' test files so it can read
    # them and understand what interface callers expect (constructor args,
    # column names, method signatures).
    all_module_tests: dict[str, list[str]] = {}
    for mod in modules:
        if mod.test_files:
            all_module_tests[mod.name] = list(mod.test_files)

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

        # Other modules' test files — for cross-module interface awareness
        other_tests = {name: files for name, files in all_module_tests.items() if name != mod.name} or None

        module_context = {
            "jump_repair_ref": rep_ref,
            "jump_validate_ref": val_ref,
            "jump_implement_ref": impl_ref,
            "test_files": mod.test_files or None,
            "owned_files": mod.files or None,
            "module_name": mod.name,
            "cross_module_tests": other_tests,
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
                "plan_output": plan_output,
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
            "plan_output": plan_output,
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
            "max_repair_attempts": limits["max_repair_attempts"],
            "max_reimplementations": limits["max_reimplementations"],
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
        repair_ref_ids.add(rep_ref)

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
        "max_repair_attempts": limits["max_repair_attempts"],
        "max_reimplementations": 2,
        "_max_jumps": limits["max_jumps"],
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
        # Depend on BOTH validate AND repair stages.  When validate
        # uses jump_to(repair), the jump handler bypasses the normal
        # CompleteStageHandler→AND-join check.  Adding repair refs
        # ensures integration starts when the last repair finishes.
        requisite_stage_ref_ids=validate_ref_ids | repair_ref_ids,
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
            "_max_jumps": limits["max_jumps"],
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
        "_max_jumps": limits["max_jumps"],
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
