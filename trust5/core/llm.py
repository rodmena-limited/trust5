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

class LLM:
    def __init__(
        self,
        model: str = "glm-4.7:cloud",
        base_url: str = "http://localhost:11434",
        timeout: int = TIMEOUT_STANDARD,
        fallback_models: list[str] | None = None,
        thinking_level: str | None = None,
        backend: str = "ollama",
        auth_header: str | None = None,
        auth_token: str | None = None,
        provider_name: str | None = None,
    ):
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.fallback_models = fallback_models or []
        self.thinking_level = thinking_level
        self.backend = backend
        emit(M.MMDL, f"model={model} backend={backend} thinking={thinking_level or 'off'}")
        self._auth_header = auth_header
        self._provider_name = provider_name
        self._abort = threading.Event()
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})
        if auth_header and auth_token:
            if auth_header == "Authorization":
                self._session.headers[auth_header] = f"Bearer {auth_token}"
            else:
                self._session.headers[auth_header] = auth_token
            if backend == "anthropic":
                self._session.headers["anthropic-version"] = "2023-06-01"
                self._session.headers["anthropic-beta"] = "oauth-2025-04-20"

    def abort(self) -> None:
        """Signal the current streaming call to stop.

        Called by a watchdog timer from another thread.  The stream
        consumers check this flag between chunks and break out cleanly.
        """
        self._abort.set()

    def reset_abort(self) -> None:
        """Clear the abort flag before starting a new LLM call."""
        self._abort.clear()

    def _stream_read_timeout(self) -> int:
        """Per-chunk read timeout, dynamic based on thinking mode."""
        if self.thinking_level:
            return STREAM_READ_TIMEOUT_THINKING
        return STREAM_READ_TIMEOUT_STANDARD

    def for_tier(
        cls,
        tier: str = "default",
        stage_name: str | None = None,
        thinking_level: str | None = None,
        **kwargs: Any,
    ) -> "LLM":
        from .auth.registry import get_active_token

        active = get_active_token()
        if active is not None:
            provider, token_data = active
            cfg = provider.config
            model = cfg.models.get(tier, cfg.models.get("default", ""))
            fallback = [m for m in cfg.fallback_chain if m != model]
            resolved = _resolve_thinking_level(tier, cfg.thinking_tiers, stage_name, thinking_level)
            return cls(
                model=model,
                base_url=cfg.api_base_url,
                fallback_models=fallback,
                thinking_level=resolved,
                backend=cfg.backend,
                auth_header=cfg.auth_header,
                auth_token=token_data.access_token,
                provider_name=cfg.name,
                **kwargs,
            )

        model = MODEL_TIERS.get(tier, MODEL_TIERS["default"])
        fallback = [m for m in DEFAULT_FALLBACK_CHAIN if m != model]
        resolved = _resolve_thinking_level(tier, THINKING_TIERS, stage_name, thinking_level)
        return cls(model=model, fallback_models=fallback, thinking_level=resolved, **kwargs)

    def chat(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        effective_model = model or self.model
        effective_timeout = timeout or self.timeout
        models_to_try = [effective_model] + [m for m in self.fallback_models if m != effective_model]

        last_error = None
        for try_model in models_to_try:
            try:
                return self._chat_with_retry(messages, tools, try_model, effective_timeout)
            except LLMError as e:
                last_error = e
                # Connection errors affect ALL models (same network) — don't
                # waste time trying fallbacks that will also fail.
                if e.error_class == "connection":
                    break
                emit(M.AFBK, f"Model {try_model} failed: {e}. Trying fallback.")
                continue

        raise LLMError(
            f"All models exhausted. Last error: {last_error}",
            retryable=last_error.retryable if last_error else False,
            error_class=last_error.error_class if last_error else "permanent",
        )

    def _chat_with_retry(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None,
        model: str,
        timeout: int,
    ) -> dict[str, Any]:
        """Retry with a time budget that varies by error class.

        Connection errors get 5 min of quick retries (5s apart).
        Server/rate errors get 3-5 min of slower retries (30s+ apart).
        """
        start = time.monotonic()
        attempt = 0
        while True:
            try:
                return self._do_chat(messages, tools, model, timeout)
            except LLMError as e:
                if not e.retryable:
                    raise
                attempt += 1
                elapsed = time.monotonic() - start

                budget: float
                delay: float
                if e.error_class == "connection":
                    budget = RETRY_BUDGET_CONNECT
                    delay = RETRY_DELAY_CONNECT
                elif e.error_class == "rate_limit":
                    budget = RETRY_BUDGET_RATE
                    delay = max(e.retry_after, float(RETRY_DELAY_SERVER))
                else:
                    budget = RETRY_BUDGET_SERVER
                    delay = max(e.retry_after, float(RETRY_DELAY_SERVER))

                remaining = budget - elapsed
                if remaining <= delay:
                    raise  # budget exhausted

                emit(
                    M.ARTY,
                    f"Retry {attempt} for {model} in {delay:.0f}s ({e.error_class}, {remaining:.0f}s budget left): {e}",
                )
                time.sleep(delay)
