from __future__ import annotations
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from .config import QualityConfig
from .quality import Issue, QualityReport
CONVENTIONAL_COMMIT_RE = re.compile(
    r"^(feat|fix|build|chore|ci|docs|style|refactor|perf|test)" r"(\([a-zA-Z0-9_./-]+\))?!?: .+$"
)
PILLAR_PASS_THRESHOLD = 0.85
PILLAR_WARNING_THRESHOLD = 0.50
