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
STAGE_THINKING_LEVEL: dict[str, str] = {
    "trust5-planner": "high",
    "planner": "high",
    "test-writer": "low",
    "test_writer": "low",
    "repairer": "low",
    "repair": "low",
}
_ANTHROPIC_THINKING_BUDGET = {"low": 5000, "high": 10000}
_GEMINI_25_THINKING_BUDGET = {"low": 5000, "high": 10000}
DEFAULT_FALLBACK_CHAIN = [
    "qwen3-coder-next:cloud",
    "kimi-k2.5:cloud",
    "nemotron-3-nano:30b-cloud",
]

def _resolve_thinking_level(
    tier: str,
    thinking_tiers: set[str],
    stage_name: str | None,
    thinking_level_override: str | None = None,
) -> str | None:
    if thinking_level_override is not None:
        return thinking_level_override
    if stage_name is not None:
        return STAGE_THINKING_LEVEL.get(stage_name.lower())
    return "low" if tier in thinking_tiers else None

class LLMError(Exception):
    """LLM call failure with error classification for smart retry logic.

    error_class values:
      "connection"  — network unreachable, DNS failure, TCP connect timeout
      "server"      — 5xx, read timeout (server alive but struggling)
      "rate_limit"  — 429 (use retry_after from server header)
      "permanent"   — 4xx, auth failure, bad request (no retry)
    """
    def __init__(
        self,
        message: str,
        retryable: bool = False,
        retry_after: float = 0,
        error_class: str = "permanent",
    ):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.error_class = error_class

    def is_network_error(self) -> bool:
        """True when the failure is infrastructure-related (not a logic error)."""
        return self.error_class in ("connection", "server", "rate_limit")
