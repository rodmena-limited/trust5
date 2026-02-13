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
RETRY_BUDGET_SERVER = 180  # 3 min: 5xx errors, overloaded backends
RETRY_BUDGET_RATE = 300  # 5 min: rate limiting (uses server's Retry-After)
RETRY_DELAY_CONNECT = 5  # quick retries — network may recover any moment
RETRY_DELAY_SERVER = 30  # give server time to recover
MODEL_CONTEXT_WINDOW: dict[str, int] = {
    "claude-opus-4-20250514": 200_000,
    "claude-sonnet-4-20250514": 200_000,
    "gemini-3-pro-preview": 1_048_576,
    "gemini-3-flash-preview": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
}
MODEL_TIERS = {
    "best": "qwen3-coder-next:cloud",
    "good": "kimi-k2.5:cloud",
    "fast": "nemotron-3-nano:30b-cloud",
    "default": "qwen3-coder-next:cloud",
}
THINKING_TIERS = {"best", "good"}
