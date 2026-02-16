import logging
import os
from typing import Any
from stabilize import StageExecution, Task, TaskResult
from ..core.config import ConfigManager, QualityConfig
from ..core.context_keys import increment_jump_count, propagate_context
from ..core.lang import LanguageProfile
from ..core.message import M, emit, emit_block
from ..core.quality import (
    QualityReport,
    TrustGate,
    is_stagnant,
    meets_quality_gate,
)
from ..core.quality_gates import (
    Assessment,
    DiagnosticSnapshot,
    MethodologyContext,
    build_snapshot_from_report,
    detect_regression,
    validate_methodology,
    validate_phase,
)
logger = logging.getLogger(__name__)
