import sys
import threading
from datetime import datetime
from enum import StrEnum
from .event_bus import (
    K_BLOCK_END,
    K_BLOCK_LINE,
    K_BLOCK_START,
    K_MSG,
    K_STREAM_END,
    K_STREAM_START,
    K_STREAM_TOKEN,
    Event,
    get_bus,
)
TOOL_CODE_MAP = {
    "Bash": M.TBSH,
    "Read": M.TRED,
    "Write": M.TWRT,
    "Edit": M.TEDT,
    "Glob": M.TGLB,
    "Grep": M.TGRP,
    "InstallPackage": M.TPKG,
    "InitProject": M.TINI,
    "AskUserQuestion": M.UASK,
}
_enabled = True
_print_fallback = True
_stream_local = threading.local()
