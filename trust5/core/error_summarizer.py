"""Fast LLM error summarizer.

Uses a fast, non-thinking model to classify and summarize raw error output
before passing it to the thinking repair agent. This reduces noise and
helps the repair agent focus on root causes instead of parsing raw logs.
"""

from __future__ import annotations

import logging

from .constants import MAX_ERROR_SUMMARY, MAX_RAW_ERROR_INPUT
from .llm import LLM, LLMError
from .message import M, emit

logger = logging.getLogger(__name__)
_MAX_RAW_INPUT = MAX_RAW_ERROR_INPUT
_MAX_SUMMARY = MAX_ERROR_SUMMARY

_SUMMARIZER_PROMPT = """You are an error classifier. Given raw test/lint/build output,
produce a structured summary. Be concise and precise.

Output format:

FAILURE_TYPE: test | lint | type_error | build | runtime | import
ROOT_CAUSE: <one sentence describing the actual problem>
FILES_AFFECTED:
- <file:line> <brief description>
SUGGESTED_FIX: <1-3 sentences on how to fix>
RAW_ERRORS:
<the most relevant 5-10 error lines, verbatim>

Rules:
- Strip stack traces to just the relevant frames
- Identify the ROOT cause, not symptoms
- If multiple errors exist, group by root cause
- If a tool is missing (e.g. "No module named X"), say so clearly
- Keep total output under 2000 characters"""


def summarize_errors(
    raw_output: str,
    failure_type: str = "test",
    timeout: int = 30,
) -> str:
    # Modern LLMs have large context. Don't summarize if output is reasonable size.
    # 32k chars is ~8k tokens, trivial for Claude/Gemini.
    if not raw_output or len(raw_output) < 32_000:
        return raw_output

    truncated = raw_output[:_MAX_RAW_INPUT]
    user_msg = f"FAILURE TYPE: {failure_type}\n\nRAW OUTPUT:\n{truncated}"

    try:
        llm = LLM.for_tier("fast", thinking_level=None)
        response = llm.chat(
            messages=[
                {"role": "system", "content": _SUMMARIZER_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            timeout=timeout,
        )
        content = response.get("content", "")
        if isinstance(content, list):
            content = "\n".join(block.get("text", "") for block in content if isinstance(block, dict))
        summary = str(content).strip()
        if summary and len(summary) > 50:
            emit(
                M.SINF,
                f"Error summarizer: {len(raw_output)} chars -> {len(summary)} chars",
            )
            return summary[:_MAX_SUMMARY]
    except LLMError as e:
        logger.warning("Error summarizer LLM failed (non-fatal): %s", e)
    except (OSError, ValueError, RuntimeError, KeyError) as e:  # summarizer: non-LLM failures
        logger.warning("Error summarizer unexpected failure (non-fatal): %s", e)

    return raw_output[:_MAX_SUMMARY]
