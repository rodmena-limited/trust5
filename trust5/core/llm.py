import json
import logging
import threading
import time
from typing import Any
import requests
from .constants import (
    STREAM_READ_TIMEOUT_STANDARD,
    STREAM_READ_TIMEOUT_THINKING,
    STREAM_TOTAL_TIMEOUT,
)
from .message import M, emit, emit_stream_end, emit_stream_start, emit_stream_token
logger = logging.getLogger(__name__)
TIMEOUT_FAST = 120
TIMEOUT_STANDARD = 300
TIMEOUT_EXTENDED = 600
CONNECT_TIMEOUT = 10
TOKEN_REFRESH_MARGIN = 300  # 5 minutes
RETRY_BUDGET_CONNECT = 300  # 5 min: network outages, DNS failures
