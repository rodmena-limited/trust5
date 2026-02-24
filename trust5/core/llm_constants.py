"""Shared LLM constants — imported by llm.py, llm_backends.py, and llm_streams.py.

This module has NO imports from the trust5 package, breaking the circular
dependency that previously required duplicating these values.
"""

from __future__ import annotations

# ── Context windows per model ────────────────────────────────────────────────

MODEL_CONTEXT_WINDOW: dict[str, int] = {
    "claude-opus-4-20250514": 200_000,
    "claude-sonnet-4-20250514": 200_000,
    "gemini-3-pro-preview": 1_048_576,
    "gemini-3-flash-preview": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
}

# ── Ollama model tiers (legacy fallback) ─────────────────────────────────────

MODEL_TIERS: dict[str, str] = {
    "best": "qwen3-coder-next:cloud",
    "good": "kimi-k2.5:cloud",
    "fast": "nemotron-3-nano:30b-cloud",
    "watchdog": "nemotron-3-nano:30b-cloud",
    "default": "qwen3-coder-next:cloud",
}

# ── Retry delays ─────────────────────────────────────────────────────────────

RETRY_DELAY_CONNECT = 5  # quick retries — network may recover any moment
RETRY_DELAY_SERVER = 10  # lower initial base — Full Jitter handles growth

# ── Thinking budgets ─────────────────────────────────────────────────────────

_ANTHROPIC_THINKING_BUDGET: dict[str, int] = {"low": 5000, "high": 10000}
_GEMINI_25_THINKING_BUDGET: dict[str, int] = {"low": 5000, "high": 10000}
