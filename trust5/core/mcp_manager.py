import json
import logging
import os
import shutil
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from .mcp import MCPClient, MCPSSEClient
from .message import M, emit
logger = logging.getLogger(__name__)
_manager: "MCPManager | None" = None
