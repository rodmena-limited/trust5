import logging
import queue
import re
import time
from typing import Any
from textual import work
from textual.app import App, ComposeResult
from textual.worker import get_current_worker
from ..core.event_bus import (
    K_BLOCK_END,
    K_BLOCK_LINE,
    K_BLOCK_START,
    K_STREAM_END,
    K_STREAM_START,
    K_STREAM_TOKEN,
    Event,
)
from ..core.message import M
from .widgets import (
    STATUS_BAR_ONLY,
    HeaderWidget,
    StatusBar0,
    StatusBar1,
    Trust5Log,
    _format_count,
    _parse_kv,
)
logger = logging.getLogger(__name__)
_BATCH_SIZE = 64
