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

    def initialize(self) -> None:
        """Load config and resolve which servers are available.

        This does NOT start any MCP servers. It only loads configuration
        and checks preconditions (like Docker availability). Idempotent.
        """
        if self._initialized:
            return
        config = self._load_config()
        servers = config.get("mcpServers", {})

        for name, server_def in servers.items():
            if server_def.get("requireDocker", False):
                if not self._check_docker():
                    logger.info("MCP server '%s' skipped (Docker not available)", name)
                    continue

            transport = server_def.get("transport", "stdio")
            if transport == "sse":
                if not server_def.get("url"):
                    logger.warning("MCP SSE server '%s' has no URL, skipping", name)
                    continue
            else:
                command = server_def.get("command", "")
                if not command:
                    logger.warning("MCP server '%s' has no command, skipping", name)
                    continue

            self._server_configs[name] = server_def

        if self._server_configs:
            names = ", ".join(self._server_configs.keys())
            emit(M.SINF, f"MCP config loaded: {len(self._server_configs)} server(s) available ({names})")

        self._initialized = True

    def create_clients(self) -> list[MCPClient | MCPSSEClient]:
        """Create and start a fresh set of MCP clients.

        Each call returns NEW client instances. The caller is responsible
        for calling stop() on each client when done (or use the
        mcp_clients() context manager for automatic cleanup).

        Failed servers are skipped with a warning, not raised.
        If some servers start but a later one fails, the already-started
        clients are still returned (not rolled back).
        """
        if not self._initialized:
            self.initialize()

        clients: list[MCPClient | MCPSSEClient] = []

        for name, server_def in self._server_configs.items():
            try:
                client = self._create_client(name, server_def)
                clients.append(client)
            except Exception as e:
                emit(M.SWRN, f"MCP server '{name}' failed to start: {e}")

        return clients
