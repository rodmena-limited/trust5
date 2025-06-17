from __future__ import annotations
import logging
from .llm import LLM, LLMError
from .message import M, emit
logger = logging.getLogger(__name__)
_MAX_RAW_INPUT = 100_000
_MAX_SUMMARY = 20_000
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
