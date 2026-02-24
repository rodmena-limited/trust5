"""LLM backend implementations (Ollama, Anthropic, Google)."""

from __future__ import annotations

import json
from typing import Any

from .llm_constants import _ANTHROPIC_THINKING_BUDGET, _GEMINI_25_THINKING_BUDGET


class LLMBackendsMixin:
    """Mixin providing backend-specific chat implementations."""

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
        if self.thinking_level:  # type: ignore[attr-defined]
            payload["think"] = True
        if tools:
            payload["tools"] = tools

        self._emit_request_log(messages, tools, model, timeout)  # type: ignore[attr-defined]

        response = self._post(f"{self.base_url}/api/chat", payload, model, timeout)  # type: ignore[attr-defined]
        return self._consume_stream(response, model)  # type: ignore[attr-defined]

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
        if self.thinking_level:  # type: ignore[attr-defined]
            budget = _ANTHROPIC_THINKING_BUDGET.get(self.thinking_level, 10000)  # type: ignore[attr-defined]
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
        if tools:
            payload["tools"] = self._convert_tools_to_anthropic(tools)

        self._emit_request_log(messages, tools, model, timeout)  # type: ignore[attr-defined]

        response = self._post(f"{self.base_url}/v1/messages", payload, model, timeout)  # type: ignore[attr-defined]
        return self._consume_anthropic_stream(response, model)  # type: ignore[attr-defined]

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
        if self.thinking_level:  # type: ignore[attr-defined]
            if "gemini-3" in model:
                gen_config["thinkingConfig"] = {"thinkingLevel": self.thinking_level.upper()}  # type: ignore[attr-defined]
            else:
                budget = _GEMINI_25_THINKING_BUDGET.get(self.thinking_level, 10000)  # type: ignore[attr-defined]
                gen_config["thinkingConfig"] = {"thinkingBudget": budget}

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": gen_config,
        }
        if system_text.strip():
            payload["systemInstruction"] = {"parts": [{"text": system_text.strip()}]}
        if tools:
            payload["tools"] = [{"functionDeclarations": self._convert_tools_to_google(tools)}]

        self._emit_request_log(messages, tools, model, timeout)  # type: ignore[attr-defined]

        url = f"{self.base_url}/v1beta/models/{model}:streamGenerateContent?alt=sse"  # type: ignore[attr-defined]
        response = self._post(url, payload, model, timeout)  # type: ignore[attr-defined]
        return self._consume_google_stream(response, model)  # type: ignore[attr-defined]

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
