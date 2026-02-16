import logging
import os
import re
import shlex
import subprocess
import time
from typing import Any
from stabilize import StageExecution, Task, TaskResult
from ..core.constants import MAX_REIMPLEMENTATIONS as _MAX_REIMPL_DEFAULT
from ..core.constants import MAX_REPAIR_ATTEMPTS as _MAX_REPAIR_DEFAULT
from ..core.constants import (
    TEST_OUTPUT_LIMIT,
)
from ..core.context_keys import check_jump_limit, increment_jump_count, propagate_context
from ..core.lang import detect_language, get_profile
from ..core.message import M, emit, emit_block
from ..core.tools import _matches_test_pattern
logger = logging.getLogger(__name__)
MAX_REPAIR_ATTEMPTS = _MAX_REPAIR_DEFAULT
MAX_REIMPLEMENTATIONS = _MAX_REIMPL_DEFAULT
_FALLBACK_EXTENSIONS = (".py", ".go", ".ts", ".js", ".rs", ".java", ".rb")
