from __future__ import annotations
import logging
from .llm import LLM, LLMError
from .message import M, emit
logger = logging.getLogger(__name__)
_MAX_RAW_INPUT = 100_000
