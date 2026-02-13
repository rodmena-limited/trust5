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

    def start(self) -> None:
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
            text=True,
        )
        self._send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "trust5", "version": "0.1.0"},
            },
        )
        resp = self._read_response()
        self.server_capabilities = resp.get("result", {}).get("capabilities", {})

        self._send_notification("notifications/initialized")

    def stop(self) -> None:
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except ProcessLookupError:
                pass  # Process already exited between poll() and kill()
            except Exception:
                try:
                    if self.process.poll() is None:
                        self.process.kill()
                except ProcessLookupError:
                    pass  # Race: process exited between poll() and kill()
            self.process = None

    def list_tools(self) -> list[dict[str, Any]]:
        resp = self._send_request("tools/list")
        result: list[dict[str, Any]] = resp.get("result", {}).get("tools", [])
        return result
