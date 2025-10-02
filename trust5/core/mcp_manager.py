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

def _stop_clients(clients: list[MCPClient | MCPSSEClient]) -> None:
    """Stop a list of MCP clients, suppressing errors."""
    for client in clients:
        try:
            client.stop()
        except Exception:
            pass

class MCPManager:
    """Loads MCP config and provides a factory for per-agent MCP clients.

    Unlike a traditional singleton that holds long-lived connections,
    this manager stores *configuration* and creates fresh client instances
    on demand via create_clients(). Each Agent gets exclusive clients,
    ensuring thread safety when Stabilize runs parallel pipeline stages.
    """
    def __init__(self, config_path: str | None = None):
        self.config_path = config_path or self._find_config()
        self._server_configs: dict[str, dict[str, Any]] = {}
        self._initialized = False
        self._docker_checked = False
        self._docker_ok = False
