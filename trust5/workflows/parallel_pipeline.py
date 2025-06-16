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
