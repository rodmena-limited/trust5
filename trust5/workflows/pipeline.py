import logging
import os
from stabilize import StageExecution, TaskExecution, Workflow
from ..core.config import ConfigManager
from ..core.lang import detect_language, get_profile
logger = logging.getLogger(__name__)
MAX_REPAIR_JUMPS = 50

def _load_development_mode(project_root: str) -> str:
    try:
        mgr = ConfigManager(project_root)
        cfg = mgr.load_config()
        return cfg.quality.development_mode
    except Exception:
        return "hybrid"
