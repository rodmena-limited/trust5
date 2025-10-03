from __future__ import annotations
import logging
import os
import re
from dataclasses import dataclass
logger = logging.getLogger(__name__)
_PASCAL_CASE_RE = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
