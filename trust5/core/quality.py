from __future__ import annotations
import ast
import json
import logging
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import Any
from pydantic import BaseModel, Field
from .config import QualityConfig
from .lang import LanguageProfile
from .message import M, emit
logger = logging.getLogger(__name__)
PRINCIPLE_TESTED = "tested"
PRINCIPLE_READABLE = "readable"
PRINCIPLE_UNDERSTANDABLE = "understandable"
PRINCIPLE_SECURED = "secured"
PRINCIPLE_TRACKABLE = "trackable"
PRINCIPLE_WEIGHTS: dict[str, float] = {
    PRINCIPLE_TESTED: 0.30,
    PRINCIPLE_READABLE: 0.15,
    PRINCIPLE_UNDERSTANDABLE: 0.15,
    PRINCIPLE_SECURED: 0.25,
    PRINCIPLE_TRACKABLE: 0.15,
}
ALL_PRINCIPLES = list(PRINCIPLE_WEIGHTS.keys())
PASS_SCORE_THRESHOLD = 0.70
