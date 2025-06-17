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

@dataclass
class ModuleSpec:
    id: str
    name: str
    files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    deps: list[str] = field(default_factory=list)
