"""LLM stream consumption implementations."""

from __future__ import annotations

import json
import time
from typing import Any

import requests

from .constants import RETRY_DELAY_SERVER, STREAM_TOTAL_TIMEOUT
from .llm_constants import MODEL_CONTEXT_WINDOW, RETRY_DELAY_CONNECT
from .llm_errors import LLMError
from .message import M, emit, emit_stream_end, emit_stream_start, emit_stream_token


class LLMStreamsMixin:
    """Mixin providing stream consumption for each backend."""

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
                if self._abort.is_set():  # type: ignore[attr-defined]
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
        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout) as e:
            raise LLMError(
                f"Stream interrupted for {model}: {e}",
                retryable=True,
                retry_after=RETRY_DELAY_SERVER,
                error_class="server",
            ) from e
        except (requests.exceptions.Timeout, OSError) as e:
            raise LLMError(
                f"Stream timeout for {model}: {e}",
                retryable=True,
                retry_after=RETRY_DELAY_CONNECT,
                error_class="connection",
            ) from e
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
                if self._abort.is_set():  # type: ignore[attr-defined]
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
                if self._abort.is_set():  # type: ignore[attr-defined]
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
