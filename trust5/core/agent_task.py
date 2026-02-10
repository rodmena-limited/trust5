import logging
import os
import threading
import time
from typing import Any
import yaml
from stabilize import StageExecution, Task, TaskResult
from stabilize.errors import TransientError
from ..core.agent import Agent
from ..core.context_builder import build_project_context
from ..core.ears import all_templates
from ..core.lang import LanguageProfile, build_language_context
from ..core.llm import LLM, LLMError
from ..core.mcp_manager import mcp_clients
from ..core.message import M, emit
logger = logging.getLogger(__name__)
NON_INTERACTIVE_PREFIX = (
    "CRITICAL: You are running inside a fully autonomous, non-interactive pipeline. "
    "There is NO human at the terminal. You MUST make all decisions autonomously "
    "using sensible defaults. NEVER attempt to ask the user questions — the "
    "AskUserQuestion tool is NOT available. If you need to make a choice, pick the "
    "most reasonable option and proceed.\n\n"
    "IMPORTANT: /testbed does NOT exist. NEVER read, write, or cd to /testbed. "
    "All file paths must be relative to the current working directory.\n\n"
)
PLANNER_TOOLS = ["Read", "ReadFiles", "Glob", "Grep"]
TEST_WRITER_TOOLS = ["Read", "ReadFiles", "Write", "Edit", "Glob", "Grep"]
_STAGE_OUTPUT_KEYS = ("plan_output", "test_writer_output", "implementer_output")
_TDD_GREEN_PHASE_INSTRUCTIONS = (
    "## TDD GREEN PHASE (auto-injected)\n\n"
    "Test files already exist from the RED phase. Your job is to:\n"
    "1. Read ALL existing test files first (use Glob to find *test* and *spec* files)\n"
    "2. Write ONLY source/implementation code to make the tests pass\n"
    "3. Do NOT create new test files — they already exist from the RED phase\n"
    "4. Do NOT modify existing test files — the tests define the specification\n"
    "5. Run tests after implementation to verify all existing tests pass\n"
    "6. If a test fails, fix the implementation — NEVER fix the test\n"
)
