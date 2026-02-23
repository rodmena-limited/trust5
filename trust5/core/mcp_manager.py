"""MCP server lifecycle manager for Trust5 pipeline agents.

Provides a module-level singleton that loads MCP server config from
.trust5/mcp.json at startup, then acts as a **factory** for creating
per-agent MCP client instances on demand.

Architecture: Each Agent gets its own exclusive set of MCP clients.
This prevents shared-state concurrency bugs when Stabilize runs
parallel pipeline stages (e.g., multi-module builds). The MCPManager
itself holds only *configuration* — never live connections or
subprocess handles.

Supports two transports:
- stdio (default): spawns a subprocess, communicates via JSON-RPC on stdin/stdout
- sse: connects to a remote SSE endpoint (e.g., Stabilize MCP server)

Docker MCP servers (requireDocker: true) are skipped when Docker is not
available, with no fallback to npx.
"""

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

# Default MCP config used when no .trust5/mcp.json exists.
# Empty by default — MCP servers are opt-in. Users can configure
# servers in .trust5/mcp.json or mcp.json in the project root.
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
        logger.debug("Docker MCP gateway status check failed", exc_info=True)
        return False


def _stop_clients(clients: list[MCPClient | MCPSSEClient]) -> None:
    """Stop a list of MCP clients, suppressing errors."""
    for client in clients:
        try:
            client.stop()
        except Exception:
            logger.debug("Failed to stop MCP client", exc_info=True)


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

    def _create_client(self, name: str, server_def: dict[str, Any]) -> MCPClient | MCPSSEClient:
        """Create, start, and return a single MCP client."""
        transport = server_def.get("transport", "stdio")

        if transport == "sse":
            client: MCPClient | MCPSSEClient = MCPSSEClient(
                url=server_def["url"],
                name=name,
            )
            client.start()
            tools = client.list_tools()
            emit(M.SINF, f"MCP server '{name}' connected via SSE ({len(tools)} tools)")
            return client
        else:
            command = self._build_command(server_def)
            env = self._build_env(server_def)
            client = MCPClient(command=command, env=env, name=name)
            client.start()
            tools = client.list_tools()
            emit(M.SINF, f"MCP server '{name}' started via stdio ({len(tools)} tools)")
            return client

    def _check_docker(self) -> bool:
        """Cached Docker availability check."""
        if not self._docker_checked:
            self._docker_ok = _docker_available()
            self._docker_checked = True
            if not self._docker_ok:
                emit(M.SINF, "Docker MCP Toolkit not available, skipping Docker MCP servers")
        return self._docker_ok

    @staticmethod
    def _find_config() -> str:
        """Locate MCP config file. Checks .trust5/ then project root."""
        candidates = [
            os.path.join(os.getcwd(), ".trust5", "mcp.json"),
            os.path.join(os.getcwd(), "mcp.json"),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        return ""  # empty = use defaults

    def _load_config(self) -> dict[str, Any]:
        if not self.config_path or not os.path.exists(self.config_path):
            return _DEFAULT_CONFIG
        try:
            with open(self.config_path, encoding="utf-8") as f:
                data: dict[str, Any] = json.load(f)
                return data
        except Exception as e:
            logger.warning("Failed to load MCP config %s: %s", self.config_path, e)
            return _DEFAULT_CONFIG

    @staticmethod
    def _build_command(server_def: dict[str, Any]) -> list[str]:
        cmd = server_def.get("command", "")
        args = server_def.get("args", [])
        return [cmd] + list(args)

    @staticmethod
    def _build_env(server_def: dict[str, Any]) -> dict[str, str] | None:
        env_overrides = server_def.get("env", {})
        if not env_overrides:
            return None
        env = os.environ.copy()
        env.update(env_overrides)
        return env


# ── Module-level API ─────────────────────────────────────────────────


def init_mcp(config_path: str | None = None) -> None:
    """Initialize the global MCP manager (loads config, checks Docker).

    Does NOT start any MCP servers. Servers are created on demand
    via create_mcp_clients() or the mcp_clients() context manager.
    """
    global _manager
    if _manager is not None:
        return
    _manager = MCPManager(config_path)
    _manager.initialize()


def shutdown_mcp() -> None:
    """Clear the global MCP manager."""
    global _manager
    _manager = None


def create_mcp_clients() -> list[MCPClient | MCPSSEClient]:
    """Create a fresh set of MCP clients from the global config.

    Returns new, exclusive instances each call. The caller MUST call
    stop() on each client when done, or use mcp_clients() instead.
    Returns empty list if no manager is initialized.
    """
    if _manager is None:
        return []
    return _manager.create_clients()


@contextmanager
def mcp_clients() -> Generator[list[MCPClient | MCPSSEClient], None, None]:
    """Context manager that creates exclusive MCP clients and ensures cleanup.

    Usage::

        with mcp_clients() as clients:
            agent = Agent(name="impl", ..., mcp_clients=clients)
            result = agent.run(user_input)
        # clients are automatically stopped here
    """
    clients = create_mcp_clients()
    try:
        yield clients
    finally:
        _stop_clients(clients)
