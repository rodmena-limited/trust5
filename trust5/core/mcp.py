import json
import logging
import os
import select
import subprocess
from typing import Any
logger = logging.getLogger(__name__)

class MCPClient:
    """JSON-RPC 2.0 stdio client for Model Context Protocol servers."""
    def __init__(
        self,
        command: list[str],
        env: dict[str, str] | None = None,
        name: str = "mcp",
        start_timeout: float = 30.0,
    ):
        self.name = name
        self.command = command
        self.env = env or os.environ.copy()
        self.start_timeout = start_timeout
        self.process: subprocess.Popen[str] | None = None
        self.server_capabilities: dict[str, Any] = {}
        self.msg_id = 0

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None
