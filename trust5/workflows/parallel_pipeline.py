from __future__ import annotations
import json
import logging
import os
import re
from dataclasses import dataclass, field
from stabilize import StageExecution, TaskExecution, Workflow
from ..core.config import ConfigManager
from ..core.lang import detect_language, get_profile
from .pipeline import MAX_REPAIR_JUMPS, _load_mutation_enabled
logger = logging.getLogger(__name__)
_MODULES_RE = re.compile(r"<!--\s*MODULES\s*\n(.*?)\n\s*-->", re.DOTALL)
_FACADE_FILES = frozenset({
    "__init__.py", "base.py", "index.py", "index.ts", "index.js",
    "mod.rs", "lib.rs", "main.go",
})

def extract_plan_output(workflow: Workflow) -> str:
    for stage in workflow.stages:
        if stage.ref_id == "plan":
            outputs = stage.outputs or {}
            return str(outputs.get("response", outputs.get("result", "")))
    return ""

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

def _load_development_mode(project_root: str) -> str:
    try:
        mgr = ConfigManager(project_root)
        cfg = mgr.load_config()
        return cfg.quality.development_mode
    except Exception:
        return "hybrid"

@dataclass
class ModuleSpec:
    id: str
    name: str
    files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    deps: list[str] = field(default_factory=list)
