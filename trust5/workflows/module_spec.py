"""Module specification parsing, validation, and data structures.

Extracts module definitions from planner output (``<!-- MODULES JSON -->``)
and validates ownership / dependency constraints before parallel execution.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

from stabilize import Workflow

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

    white, gray, black = 0, 1, 2
    color: dict[str, int] = {mid: white for mid in module_ids}

    for start in module_ids:
        if color[start] != white:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        color[start] = gray
        while stack:
            node, idx = stack.pop()
            children = deps_map.get(node, [])
            if idx < len(children):
                stack.append((node, idx + 1))
                child = children[idx]
                if color[child] == gray:
                    raise ValueError(
                        f"Module dependency cycle detected involving {child!r}. "
                        f"Stabilize would deadlock. Fix the module dependency graph."
                    )
                if color[child] == white:
                    color[child] = gray
                    stack.append((child, 0))
            else:
                color[node] = black


_FACADE_FILES = frozenset(
    {
        "__init__.py",
        "base.py",
        "index.py",
        "index.ts",
        "index.js",
        "mod.rs",
        "lib.rs",
        "main.go",
    }
)


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
