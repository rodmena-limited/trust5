import logging
import threading
import time
from datetime import timedelta
from fractions import Fraction
from typing import Any

import requests
from resilient_circuit import CircuitProtectorPolicy, ExponentialDelay
from resilient_circuit.exceptions import ProtectedCallError

from .constants import (
    LLM_CONNECT_TIMEOUT,
    LLM_MAX_BACKOFF_DELAY,
    LLM_RETRY_BUDGET_CONNECT,
    LLM_RETRY_BUDGET_RATE,
    LLM_RETRY_BUDGET_SERVER,
    LLM_TIMEOUT_EXTENDED,
    LLM_TIMEOUT_FAST,
    LLM_TIMEOUT_STANDARD,
    LLM_TOKEN_REFRESH_MARGIN,
    STREAM_READ_TIMEOUT_STANDARD,
    STREAM_READ_TIMEOUT_THINKING,
)
from .llm_backends import LLMBackendsMixin
from .llm_errors import LLMError as LLMError  # noqa: F401 — re-export for backward compat
from .llm_streams import LLMStreamsMixin
from .message import M, emit

logger = logging.getLogger(__name__)

TIMEOUT_FAST = LLM_TIMEOUT_FAST
TIMEOUT_STANDARD = LLM_TIMEOUT_STANDARD
TIMEOUT_EXTENDED = LLM_TIMEOUT_EXTENDED

CONNECT_TIMEOUT = LLM_CONNECT_TIMEOUT
TOKEN_REFRESH_MARGIN = LLM_TOKEN_REFRESH_MARGIN
# Retry budgets (total seconds per error class before giving up)
RETRY_BUDGET_CONNECT = LLM_RETRY_BUDGET_CONNECT
RETRY_BUDGET_SERVER = LLM_RETRY_BUDGET_SERVER
RETRY_BUDGET_RATE = LLM_RETRY_BUDGET_RATE
RETRY_DELAY_CONNECT = 5  # quick retries — network may recover any moment (unchanged)
RETRY_DELAY_SERVER = 10  # lower initial base — Full Jitter handles growth
MAX_BACKOFF_DELAY = LLM_MAX_BACKOFF_DELAY

# Backoff strategies per error class (using resilient-circuit)
_BACKOFF_CONNECT = ExponentialDelay(
    min_delay=timedelta(seconds=5),  # RETRY_DELAY_CONNECT
    max_delay=timedelta(seconds=300),  # MAX_BACKOFF_DELAY
    factor=2,
    jitter=1.0,  # Full-spread jitter: delay ∈ [0, 2 × base × 2^(attempt-1)]
)
_BACKOFF_SERVER = ExponentialDelay(
    min_delay=timedelta(seconds=10),  # RETRY_DELAY_SERVER
    max_delay=timedelta(seconds=300),  # MAX_BACKOFF_DELAY
    factor=2,
    jitter=1.0,
)


# ── Model circuit breakers ───────────────────────────────────────────
# Prevents wasting retry budget on models that consistently fail.
# Circuit opens after 3/5 failures, closes after 2 consecutive successes.
# 2-minute cooldown before half-open probe.

_model_circuits: dict[str, CircuitProtectorPolicy] = {}


def _get_model_circuit(model: str) -> CircuitProtectorPolicy:
    """Get or create a circuit breaker for a specific model endpoint."""
    if model not in _model_circuits:
        _model_circuits[model] = CircuitProtectorPolicy(
            resource_key=f"trust5-llm-{model}",
            cooldown=timedelta(seconds=120),
            failure_limit=Fraction(3, 5),
            success_limit=Fraction(2, 2),
        )
    return _model_circuits[model]


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
    "watchdog": "nemotron-3-nano:30b-cloud",
    "default": "qwen3-coder-next:cloud",
}

THINKING_TIERS = {"best", "good"}

# Per-stage thinking levels: None=off, "low", "high"
# Planner needs deep reasoning; test-writer needs some; implementer needs max output tokens.
STAGE_THINKING_LEVEL: dict[str, str] = {
    "trust5-planner": "high",
    "planner": "high",
    "test-writer": "low",
    "test_writer": "low",
    "repairer": "low",
    "repair": "low",
}

# Anthropic thinking budget mapped from level
_ANTHROPIC_THINKING_BUDGET = {"low": 5000, "high": 10000}

# Gemini 2.5 thinking budget mapped from level
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


class LLM(LLMBackendsMixin, LLMStreamsMixin):
    """Multi-provider LLM client with streaming, retry, and circuit breaker.

    Supports Anthropic (Claude), Google (Gemini), and Ollama backends.
    Includes automatic model fallback, exponential backoff on failures,
    and per-chunk abort signaling for watchdog integration.
    """

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
        self._token_lock = threading.Lock()
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

    # ── Abort / watchdog support ─────────────────────────────────────────────

    def abort(self) -> None:
        """Signal the current streaming call to stop.

        Called by a watchdog timer from another thread.  The stream
        consumers check this flag between chunks and break out cleanly.
        """
        self._abort.set()

    def reset_abort(self) -> None:
        """Clear the abort flag before starting a new LLM call."""
        self._abort.clear()

    @property
    def _stream_read_timeout(self) -> int:
        """Per-chunk read timeout, dynamic based on thinking mode."""
        if self.thinking_level:
            return STREAM_READ_TIMEOUT_THINKING
        return STREAM_READ_TIMEOUT_STANDARD

    @classmethod
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
            circuit = _get_model_circuit(try_model)
            try:
                circuit._status.validate_execution()
            except ProtectedCallError:
                emit(M.AFBK, f"Circuit open for {try_model}, skipping to fallback.")
                continue

            try:
                result = self._chat_with_retry(messages, tools, try_model, effective_timeout)
                circuit._status.mark_success()
                circuit._save_state()
                return result
            except LLMError as e:
                last_error = e
                if e.retryable:
                    if circuit.should_consider_failure(e):
                        circuit._status.mark_failure()
                        circuit._save_state()
                if e.error_class in ("connection", "auth"):
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
        """Retry with Full Jitter exponential backoff and time budgets.

        Connection errors get 1 hour of retries (5s base, jittered up to 5 min).
        Server errors get 30 min (10s base, jittered).
        Rate limit errors get 1 hour (respect server Retry-After, jittered).
        Sleep is interruptible via ``self._abort`` so watchdog can cancel.
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
                delay: float
                if e.error_class == "connection":
                    budget = RETRY_BUDGET_CONNECT
                    delay = _BACKOFF_CONNECT.for_attempt(attempt)
                elif e.error_class == "rate_limit":
                    budget = RETRY_BUDGET_RATE
                    delay = max(e.retry_after, _BACKOFF_SERVER.for_attempt(attempt))
                else:
                    budget = RETRY_BUDGET_SERVER
                    delay = _BACKOFF_SERVER.for_attempt(attempt)
                remaining = budget - elapsed
                if remaining <= delay:
                    raise  # budget exhausted
                emit(
                    M.ARTY,
                    f"Retry {attempt} for {model} in {delay:.0f}s ({e.error_class}, {remaining:.0f}s budget left): {e}",
                )
                # Interruptible sleep: abort() wakes this immediately
                if self._abort.wait(timeout=delay):
                    raise  # aborted during retry sleep

    def _do_chat(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None,
        model: str,
        timeout: int,
    ) -> dict[str, Any]:
        if self.backend == "anthropic":
            return self._do_chat_anthropic(messages, tools, model, timeout)
        if self.backend == "google":
            return self._do_chat_google(messages, tools, model, timeout)
        return self._do_chat_ollama(messages, tools, model, timeout)

    def _emit_request_log(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None,
        model: str,
        timeout: int,
    ) -> None:
        msg_roles = [m.get("role", "?") for m in messages]
        role_counts = {r: msg_roles.count(r) for r in set(msg_roles)}
        emit(
            M.CREQ,
            f"LLM request model={model} msgs={len(messages)} "
            f"roles={role_counts} tools={len(tools or [])} "
            f"timeout={timeout}s",
        )

    def _post(self, url: str, payload: dict[str, Any], model: str, timeout: int) -> requests.Response:
        self._ensure_token_fresh()
        read_timeout = self._stream_read_timeout
        try:
            response = self._session.post(
                url,
                json=payload,
                timeout=(CONNECT_TIMEOUT, read_timeout),
                stream=True,
            )
        except requests.exceptions.ConnectTimeout:
            raise LLMError(
                f"Connection timeout ({CONNECT_TIMEOUT}s) for {model}",
                retryable=True,
                retry_after=RETRY_DELAY_CONNECT,
                error_class="connection",
            )
        except requests.exceptions.ReadTimeout:
            raise LLMError(
                f"Read timeout ({read_timeout}s) for {model}",
                retryable=True,
                retry_after=RETRY_DELAY_SERVER,
                error_class="server",
            )
        except requests.exceptions.ConnectionError:
            raise LLMError(
                f"Connection error for {model} (server unreachable)",
                retryable=True,
                retry_after=RETRY_DELAY_CONNECT,
                error_class="connection",
            )
        except requests.exceptions.Timeout:
            raise LLMError(
                f"Timeout for {model}",
                retryable=True,
                retry_after=RETRY_DELAY_CONNECT,
                error_class="connection",
            )
        except requests.exceptions.RequestException as e:
            raise LLMError(
                f"Request failed for {model}: {e}",
                retryable=False,
                error_class="permanent",
            )

        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "60"))
            raise LLMError(
                f"Rate limited on {model}",
                retryable=True,
                retry_after=retry_after,
                error_class="rate_limit",
            )

        if response.status_code >= 500:
            raise LLMError(
                f"Server error {response.status_code} on {model}",
                retryable=True,
                retry_after=RETRY_DELAY_SERVER,
                error_class="server",
            )

        if response.status_code == 401 and self._auth_header and self._provider_name:
            refreshed = self._try_refresh_token()
            if refreshed:
                emit(
                    M.ARTY,
                    f"Token refreshed for {self._provider_name}, retrying request",
                )
                try:
                    response = self._session.post(
                        url,
                        json=payload,
                        timeout=(CONNECT_TIMEOUT, read_timeout),
                        stream=True,
                    )
                except requests.exceptions.RequestException as e:
                    raise LLMError(
                        f"Retry after refresh failed for {model}: {e}",
                        retryable=False,
                        error_class="permanent",
                    )
                if response.status_code == 401:
                    raise LLMError(
                        f"Authentication failed for {model} even after token refresh",
                        retryable=True,
                        retry_after=120,
                        error_class="auth",
                    )
            else:
                raise LLMError(
                    f"Authentication failed for {model} (401) and token refresh failed",
                    retryable=True,
                    retry_after=120,
                    error_class="auth",
                )

        if response.status_code != 200:
            raise LLMError(
                f"HTTP {response.status_code} from {model}: {response.text[:200]}",
                retryable=False,
                error_class="permanent",
            )

        return response

    def _ensure_token_fresh(self) -> None:
        if not self._provider_name or not self._auth_header:
            return
        with self._token_lock:
            try:
                from .auth.token_store import TokenStore

                store = TokenStore()
                token_data = store.load(self._provider_name)
                if token_data is None or token_data.expires_at is None:
                    return
                remaining = token_data.expires_at - time.time()
                if remaining > TOKEN_REFRESH_MARGIN:
                    return
                emit(M.ARTY, f"Token expires in {remaining:.0f}s, refreshing proactively")
                self._try_refresh_token_locked()
            except Exception:
                logger.debug("Proactive token refresh check failed", exc_info=True)

    def _try_refresh_token(self) -> bool:
        with self._token_lock:
            return self._try_refresh_token_locked()

    def _try_refresh_token_locked(self) -> bool:
        """Refresh token with retry. Must be called with _token_lock held."""
        if not self._provider_name or not self._auth_header:
            return False

        from .auth.registry import get_provider
        from .auth.token_store import TokenStore

        provider = get_provider(self._provider_name)
        store = TokenStore()
        token_data = store.load(self._provider_name)
        if token_data is None:
            return False

        _refresh_backoff = ExponentialDelay(
            min_delay=timedelta(seconds=2),
            max_delay=timedelta(seconds=15),
            factor=2,
            jitter=0.2,
        )
        max_refresh_attempts = 3
        last_exc: Exception | None = None
        for attempt in range(1, max_refresh_attempts + 1):
            try:
                new_token = provider.refresh(token_data)
                store.save(self._provider_name, new_token)
                if self._auth_header == "Authorization":
                    self._session.headers[self._auth_header] = f"Bearer {new_token.access_token}"
                else:
                    self._session.headers[self._auth_header] = new_token.access_token
                logger.info("Token refreshed mid-pipeline for %s (attempt %d)", self._provider_name, attempt)
                return True
            except requests.exceptions.ConnectionError as e:
                last_exc = e
                logger.debug("Transient refresh error (attempt %d): %s", attempt, e)
                time.sleep(_refresh_backoff.for_attempt(attempt))
            except requests.exceptions.Timeout as e:
                last_exc = e
                logger.debug("Transient refresh error (attempt %d): %s", attempt, e)
                time.sleep(_refresh_backoff.for_attempt(attempt))
            except requests.exceptions.HTTPError as e:
                # Permanent errors (invalid_grant, bad client credentials) — stop immediately
                logger.warning("Permanent refresh error for %s: %s", self._provider_name, e)
                return False
            except Exception as e:
                # Unknown errors — treat as permanent to avoid infinite loops
                logger.warning("Token refresh failed for %s: %s", self._provider_name, e, exc_info=True)
                return False

        logger.warning(
            "Token refresh exhausted retries for %s: %s",
            self._provider_name,
            last_exc,
        )
        return False

    def generate(self, prompt: str, model: str | None = None) -> str:
        effective_model = model or self.model
        payload = {
            "model": effective_model,
            "prompt": prompt,
            "stream": False,
        }
        try:
            response = self._session.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=(CONNECT_TIMEOUT, self.timeout),
            )
            response.raise_for_status()
            result: str = response.json().get("response", "")
            return result
        except requests.exceptions.ConnectTimeout:
            raise LLMError(
                f"Generation connect timeout ({CONNECT_TIMEOUT}s) for {effective_model}",
                retryable=True,
                retry_after=RETRY_DELAY_CONNECT,
                error_class="connection",
            )
        except requests.exceptions.ConnectionError:
            raise LLMError(
                f"Generation connection error for {effective_model}",
                retryable=True,
                retry_after=RETRY_DELAY_CONNECT,
                error_class="connection",
            )
        except requests.exceptions.RequestException as e:
            raise LLMError(
                f"Generation failed for {effective_model}: {e}",
                retryable=True,
                error_class="server",
            )
