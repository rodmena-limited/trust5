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
_DEFAULT_CONFIG: dict[str, Any] = {"mcpServers": {}}

def _docker_available() -> bool:
    """Check if Docker with MCP Toolkit is available."""
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(
            ["docker", "mcp", "gateway", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False
