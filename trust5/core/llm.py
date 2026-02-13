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

# Connection timeout: max seconds to establish TCP connection.
# Keep short — if the server is unreachable, fail fast and retry.
CONNECT_TIMEOUT = 10

# Per-chunk read timeout is now dynamic — see _stream_read_timeout property.
# Thinking models (Ollama think=True, Anthropic extended-thinking) need long
# pauses between chunks; non-thinking models should fail fast.

# Refresh token if it expires within this many seconds.
TOKEN_REFRESH_MARGIN = 300  # 5 minutes

# ── Retry budgets ─────────────────────────────────────────────────────────
# Total seconds to spend retrying per error class before giving up.
# Combined with Stabilize-level retries (120s wait + another cycle), this
# covers 20+ minutes of downtime: 5 min inner + 2 min wait + 5 min inner + ...
RETRY_BUDGET_CONNECT = 300  # 5 min: network outages, DNS failures
RETRY_BUDGET_SERVER = 180  # 3 min: 5xx errors, overloaded backends
RETRY_BUDGET_RATE = 300  # 5 min: rate limiting (uses server's Retry-After)

# Delay between retries per error class
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

    @property
    def is_network_error(self) -> bool:
        """True when the failure is infrastructure-related (not a logic error)."""
        return self.error_class in ("connection", "server", "rate_limit")


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

    def _do_chat_ollama(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None,
        model: str,
        timeout: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        if self.thinking_level:
            payload["think"] = True
        if tools:
            payload["tools"] = tools

        self._emit_request_log(messages, tools, model, timeout)

        response = self._post(f"{self.base_url}/api/chat", payload, model, timeout)
        return self._consume_stream(response, model)

    def _do_chat_anthropic(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        timeout: int,
    ) -> dict[str, Any]:
        system_text = ""
        api_messages: list[dict[str, Any]] = []
        tool_use_id_counter = 0

        for msg in messages:
            role = msg.get("role", "")

            if role == "system":
                system_text += msg.get("content", "") + "\n"

            elif role == "assistant":
                content_blocks: list[dict[str, Any]] = []
                text = msg.get("content", "")
                if text:
                    content_blocks.append({"type": "text", "text": text})
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    tool_use_id_counter += 1
                    tc_id = tc.get("id", f"toolu_{tool_use_id_counter:04d}")
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                    except (json.JSONDecodeError, ValueError):
                        args = {}
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc_id,
                            "name": fn.get("name", ""),
                            "input": args,
                        }
                    )
                api_messages.append(
                    {
                        "role": "assistant",
                        "content": content_blocks if content_blocks else text,
                    }
                )

            elif role == "tool":
                tool_use_id_counter_ref = tool_use_id_counter
                tc_id = msg.get("tool_call_id", f"toolu_{tool_use_id_counter_ref:04d}")
                api_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tc_id,
                                "content": msg.get("content", ""),
                            }
                        ],
                    }
                )

            else:
                api_messages.append(msg)

        payload: dict[str, Any] = {
            "model": model,
            "messages": api_messages,
            "max_tokens": 16384,
            "stream": True,
        }
        if system_text.strip():
            payload["system"] = system_text.strip()
        if self.thinking_level:
            budget = _ANTHROPIC_THINKING_BUDGET.get(self.thinking_level, 10000)
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
        if tools:
            payload["tools"] = self._convert_tools_to_anthropic(tools)

        self._emit_request_log(messages, tools, model, timeout)

        response = self._post(f"{self.base_url}/v1/messages", payload, model, timeout)
        return self._consume_anthropic_stream(response, model)

    @staticmethod
    def _convert_tools_to_anthropic(
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        converted = []
        for tool in tools:
            fn = tool.get("function", tool)
            converted.append(
                {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {}),
                }
            )
        return converted

    def _do_chat_google(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        timeout: int,
    ) -> dict[str, Any]:
        system_text = ""
        contents: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "")

            if role == "system":
                system_text += msg.get("content", "") + "\n"

            elif role == "assistant":
                parts: list[dict[str, Any]] = []
                text = msg.get("content", "")
                if text:
                    parts.append({"text": text})
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                    except (json.JSONDecodeError, ValueError):
                        args = {}
                    fc_part: dict[str, Any] = {"functionCall": {"name": fn.get("name", ""), "args": args}}
                    if tc.get("thought_signature"):
                        fc_part["thoughtSignature"] = tc["thought_signature"]
                    parts.append(fc_part)
                if parts:
                    contents.append({"role": "model", "parts": parts})

            elif role == "tool":
                tool_name = msg.get("name", "unknown")
                raw_content = msg.get("content", "")
                try:
                    response_data = json.loads(raw_content)
                    # Gemini requires response to be a dict (Struct), not a list
                    if isinstance(response_data, list):
                        response_data = {"result": response_data}
                except (json.JSONDecodeError, ValueError):
                    response_data = {"result": raw_content}
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": tool_name,
                                    "response": response_data,
                                }
                            }
                        ],
                    }
                )

            else:
                text = msg.get("content", "")
                contents.append({"role": "user", "parts": [{"text": text}]})

        gen_config: dict[str, Any] = {"maxOutputTokens": 16384}
        if self.thinking_level:
            if "gemini-3" in model:
                gen_config["thinkingConfig"] = {"thinkingLevel": self.thinking_level.upper()}
            else:
                budget = _GEMINI_25_THINKING_BUDGET.get(self.thinking_level, 10000)
                gen_config["thinkingConfig"] = {"thinkingBudget": budget}

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": gen_config,
        }
        if system_text.strip():
            payload["systemInstruction"] = {"parts": [{"text": system_text.strip()}]}
        if tools:
            payload["tools"] = [{"functionDeclarations": self._convert_tools_to_google(tools)}]

        self._emit_request_log(messages, tools, model, timeout)

        url = f"{self.base_url}/v1beta/models/{model}:streamGenerateContent?alt=sse"
        response = self._post(url, payload, model, timeout)
        return self._consume_google_stream(response, model)

    @staticmethod
    def _convert_tools_to_google(
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        converted = []
        for tool in tools:
            fn = tool.get("function", tool)
            converted.append(
                {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                }
            )
        return converted

    def _consume_google_stream(self, response: requests.Response, model: str) -> dict[str, Any]:
        content_parts: list[str] = []
        tool_calls_agg: list[dict[str, Any]] = []
        thinking_started = False
        response_started = False
        input_tokens = 0
        output_tokens = 0
        stream_start = time.monotonic()

        try:
            for raw_line in response.iter_lines(decode_unicode=True):
                if self._abort.is_set():
                    emit(M.SWRN, f"[{model}] LLM call aborted by watchdog")
                    break
                if time.monotonic() - stream_start > STREAM_TOTAL_TIMEOUT:
                    emit(M.SWRN, f"[{model}] Stream total timeout ({STREAM_TOTAL_TIMEOUT}s)")
                    break
                if not raw_line or not raw_line.startswith("data: "):
                    continue

                try:
                    data = json.loads(raw_line[6:])
                except (json.JSONDecodeError, ValueError):
                    continue

                if "error" in data:
                    err = data["error"]
                    raise LLMError(
                        f"Gemini stream error: {err.get('message', str(err))}",
                        retryable=True,
                        retry_after=10,
                        error_class="server",
                    )

                # Capture usage metadata (present in final chunk, but safe to overwrite)
                usage_meta = data.get("usageMetadata", {})
                if usage_meta:
                    input_tokens = usage_meta.get("promptTokenCount", input_tokens)
                    # candidatesTokenCount excludes thinking tokens;
                    # add thoughtsTokenCount for accurate output totals.
                    candidates_tok = usage_meta.get("candidatesTokenCount", 0)
                    thoughts_tok = usage_meta.get("thoughtsTokenCount", 0)
                    output_tokens = candidates_tok + thoughts_tok

                candidates = data.get("candidates", [])
                if not candidates:
                    continue

                parts = candidates[0].get("content", {}).get("parts", [])
                for part in parts:
                    if part.get("thought"):
                        text = part.get("text", "")
                        if text:
                            if not thinking_started:
                                emit_stream_start(M.ATHK, f"[{model}] Thinking")
                                thinking_started = True
                            emit_stream_token(text)

                    elif "text" in part:
                        text = part["text"]
                        if text:
                            # Close thinking stream before starting response
                            if thinking_started:
                                emit_stream_end()
                                thinking_started = False
                            if not response_started:
                                emit_stream_start(M.ARSP, f"[{model}] ")
                                response_started = True
                            emit_stream_token(text)
                            content_parts.append(text)

                    elif "functionCall" in part:
                        fc = part["functionCall"]
                        tc: dict[str, Any] = {
                            "id": f"call_{len(tool_calls_agg):04d}",
                            "function": {
                                "name": fc.get("name", ""),
                                "arguments": json.dumps(fc.get("args", {})),
                            },
                        }
                        if part.get("thoughtSignature"):
                            tc["thought_signature"] = part["thoughtSignature"]
                        tool_calls_agg.append(tc)
        finally:
            if thinking_started:
                emit_stream_end()
            if response_started:
                emit_stream_end()
            response.close()

        full_content = "".join(content_parts)
        assembled_msg: dict[str, Any] = {
            "role": "assistant",
            "content": full_content,
        }
        if tool_calls_agg:
            assembled_msg["tool_calls"] = tool_calls_agg

        tc_count = len(tool_calls_agg)
        total_tokens = input_tokens + output_tokens
        emit(
            M.CRES,
            f"LLM response model={model} content={len(full_content)} chars tool_calls={tc_count} tokens={total_tokens}",
        )
        emit(M.MTKN, f"in={input_tokens} out={output_tokens} total={total_tokens}")
        ctx_window = MODEL_CONTEXT_WINDOW.get(model, 1_048_576)
        emit(M.MCTX, f"used={input_tokens} remaining={ctx_window - input_tokens} window={ctx_window}")

        return {"message": assembled_msg, "done": True}

    def _consume_anthropic_stream(self, response: requests.Response, model: str) -> dict[str, Any]:
        content_parts: list[str] = []
        tool_calls_agg: list[dict[str, Any]] = []
        thinking_started = False
        response_started = False
        current_tool: dict[str, Any] = {}
        input_json_parts: list[str] = []
        input_tokens = 0
        output_tokens = 0
        stream_start = time.monotonic()

        try:
            for raw_line in response.iter_lines(decode_unicode=True):
                if self._abort.is_set():
                    emit(M.SWRN, f"[{model}] LLM call aborted by watchdog")
                    break
                if time.monotonic() - stream_start > STREAM_TOTAL_TIMEOUT:
                    emit(M.SWRN, f"[{model}] Stream total timeout ({STREAM_TOTAL_TIMEOUT}s)")
                    break
                if not raw_line:
                    continue
                if raw_line.startswith("event: "):
                    event_type = raw_line[7:].strip()
                    if event_type == "message_stop":
                        break
                    continue
                if not raw_line.startswith("data: "):
                    continue

                try:
                    data = json.loads(raw_line[6:])
                except (json.JSONDecodeError, ValueError):
                    continue

                evt = data.get("type", "")

                if evt == "message_start":
                    usage = data.get("message", {}).get("usage", {})
                    input_tokens = usage.get("input_tokens", 0)

                elif evt == "content_block_start":
                    block = data.get("content_block", {})
                    if block.get("type") == "tool_use":
                        current_tool = {
                            "id": block.get("id", ""),
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": "",
                            },
                        }
                        input_json_parts = []

                elif evt == "content_block_delta":
                    delta = data.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            # Close thinking stream before starting response
                            if thinking_started:
                                emit_stream_end()
                                thinking_started = False
                            if not response_started:
                                emit_stream_start(M.ARSP, f"[{model}] ")
                                response_started = True
                            emit_stream_token(text)
                            content_parts.append(text)
                    elif delta.get("type") == "thinking_delta":
                        text = delta.get("thinking", "")
                        if text:
                            if not thinking_started:
                                emit_stream_start(M.ATHK, f"[{model}] Thinking")
                                thinking_started = True
                            emit_stream_token(text)
                    elif delta.get("type") == "input_json_delta":
                        input_json_parts.append(delta.get("partial_json", ""))

                elif evt == "content_block_stop":
                    if current_tool:
                        raw_json = "".join(input_json_parts)
                        current_tool["function"]["arguments"] = raw_json
                        tool_calls_agg.append(current_tool)
                        current_tool = {}
                        input_json_parts = []

                elif evt == "message_delta":
                    delta_usage = data.get("usage", {})
                    output_tokens = delta_usage.get("output_tokens", output_tokens)

                elif evt == "error":
                    err = data.get("error", {})
                    raise LLMError(
                        f"Anthropic stream error: {err.get('message', str(err))}",
                        retryable=True,
                        retry_after=10,
                        error_class="server",
                    )
        finally:
            if thinking_started:
                emit_stream_end()
            if response_started:
                emit_stream_end()
            response.close()

        full_content = "".join(content_parts)
        assembled_msg: dict[str, Any] = {
            "role": "assistant",
            "content": full_content,
        }
        if tool_calls_agg:
            assembled_msg["tool_calls"] = tool_calls_agg

        tc_count = len(tool_calls_agg)
        total_tokens = input_tokens + output_tokens
        emit(
            M.CRES,
            f"LLM response model={model} content={len(full_content)} chars tool_calls={tc_count} tokens={total_tokens}",
        )
        emit(M.MTKN, f"in={input_tokens} out={output_tokens} total={total_tokens}")
        ctx_window = MODEL_CONTEXT_WINDOW.get(model, 200_000)
        emit(M.MCTX, f"used={input_tokens} remaining={ctx_window - input_tokens} window={ctx_window}")

        return {"message": assembled_msg, "done": True}

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
                        retryable=False,
                        error_class="permanent",
                    )
            else:
                raise LLMError(
                    f"Authentication failed for {model} (401) and token refresh failed",
                    retryable=False,
                    error_class="permanent",
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
            self._try_refresh_token()
        except Exception:
            logger.debug("Proactive token refresh check failed", exc_info=True)

    def _try_refresh_token(self) -> bool:
        if not self._provider_name or not self._auth_header:
            return False
        try:
            from .auth.registry import get_provider
            from .auth.token_store import TokenStore

            provider = get_provider(self._provider_name)
            store = TokenStore()
            token_data = store.load(self._provider_name)
            if token_data is None:
                return False

            new_token = provider.refresh(token_data)
            store.save(self._provider_name, new_token)
            if self._auth_header == "Authorization":
                self._session.headers[self._auth_header] = f"Bearer {new_token.access_token}"
            else:
                self._session.headers[self._auth_header] = new_token.access_token
            logger.info("Token refreshed mid-pipeline for %s", self._provider_name)
            return True
        except Exception:
            logger.warning("Token refresh failed for %s", self._provider_name, exc_info=True)
            return False

    def _consume_stream(
        self,
        response: requests.Response,
        model: str,
    ) -> dict[str, Any]:
        content_parts: list[str] = []
        tool_calls_agg: list[dict[str, Any]] = []
        final_data: dict[str, Any] = {}
        thinking_started = False
        response_started = False
        stream_start = time.monotonic()

        try:
            for raw_line in response.iter_lines(decode_unicode=True):
                if self._abort.is_set():
                    emit(M.SWRN, f"[{model}] LLM call aborted by watchdog")
                    break
                if time.monotonic() - stream_start > STREAM_TOTAL_TIMEOUT:
                    emit(M.SWRN, f"[{model}] Stream total timeout ({STREAM_TOTAL_TIMEOUT}s)")
                    break
                if not raw_line:
                    continue
                try:
                    chunk = json.loads(raw_line)
                except (json.JSONDecodeError, ValueError):
                    continue

                if "error" in chunk and "message" not in chunk:
                    raise LLMError(
                        f"API error from {model}: {chunk['error']}",
                        retryable=True,
                        retry_after=10,
                        error_class="server",
                    )

                msg = chunk.get("message", {})

                # Ollama thinking content (think=True mode)
                thinking = msg.get("thinking", "")
                if thinking:
                    if not thinking_started:
                        emit_stream_start(M.ATHK, f"[{model}] Thinking")
                        thinking_started = True
                    emit_stream_token(thinking)

                # Ollama response content
                delta = msg.get("content", "")
                if delta:
                    if thinking_started:
                        emit_stream_end()
                        thinking_started = False
                    if not response_started:
                        emit_stream_start(M.ARSP, f"[{model}] ")
                        response_started = True
                    emit_stream_token(delta)
                    content_parts.append(delta)

                chunk_tc = msg.get("tool_calls", [])
                if chunk_tc:
                    tool_calls_agg.extend(chunk_tc)

                if chunk.get("done", False):
                    final_data = chunk
                    break
        finally:
            if thinking_started:
                emit_stream_end()
            if response_started:
                emit_stream_end()
            response.close()

        full_content = "".join(content_parts)
        assembled_msg: dict[str, Any] = {
            "role": "assistant",
            "content": full_content,
        }
        if tool_calls_agg:
            assembled_msg["tool_calls"] = tool_calls_agg

        result: dict[str, Any] = {**final_data, "message": assembled_msg}

        tc_count = len(tool_calls_agg)
        input_tokens = final_data.get("prompt_eval_count", 0)
        output_tokens = final_data.get("eval_count", 0)
        total_tokens = input_tokens + output_tokens
        emit(
            M.CRES,
            f"LLM response model={model} content={len(full_content)} chars tool_calls={tc_count} tokens={total_tokens}",
        )
        if input_tokens or output_tokens:
            emit(M.MTKN, f"in={input_tokens} out={output_tokens} total={total_tokens}")
            ctx_window = MODEL_CONTEXT_WINDOW.get(model, 128_000)
            emit(M.MCTX, f"used={input_tokens} remaining={ctx_window - input_tokens} window={ctx_window}")

        return result

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
