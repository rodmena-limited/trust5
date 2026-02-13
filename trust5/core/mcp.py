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

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        resp = self._send_request("tools/call", {"name": name, "arguments": arguments})
        result: dict[str, Any] | None = resp.get("result")
        return result

    def _send_request(self, method: str, params: Any = None) -> dict[str, Any]:
        self.msg_id += 1
        req = {"jsonrpc": "2.0", "id": self.msg_id, "method": method}
        if params is not None:
            req["params"] = params

        self._write_json(req)
        return self._read_response()

    def _send_notification(self, method: str, params: Any = None) -> None:
        req: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            req["params"] = params
        self._write_json(req)

    def _write_json(self, data: dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            raise RuntimeError(f"MCP server '{self.name}' not running")

        json_str = json.dumps(data)
        self.process.stdin.write(json_str + "\n")
        self.process.stdin.flush()

    def _read_response(self) -> dict[str, Any]:
        if not self.process or not self.process.stdout:
            raise RuntimeError(f"MCP server '{self.name}' not running")

        fd = self.process.stdout.fileno()
        ready, _, _ = select.select([fd], [], [], self.start_timeout)
        if not ready:
            raise RuntimeError(f"MCP server '{self.name}' timed out after {self.start_timeout}s")

        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(f"MCP server '{self.name}' closed connection")

        result: dict[str, Any] = json.loads(line)
        return result

class MCPSSEClient:
    """MCP client using SSE (Server-Sent Events) transport.

    Fully synchronous — no threading. Trust5 agents process tool calls
    one at a time, so we simply POST a request then read the SSE stream
    until the matching response arrives.

    SSE protocol flow:
    1. GET /sse -> server opens SSE stream, sends 'endpoint' event with POST URL
    2. Client POSTs JSON-RPC to that URL
    3. Server sends response as SSE 'message' event with matching id
    """
    def __init__(
        self,
        url: str,
        name: str = "mcp-sse",
        timeout: float = 30.0,
    ):
        self.url = url
        self.name = name
        self.timeout = timeout
        self.msg_id = 0
        self._message_url: str | None = None
        self._session: Any = None
        self._sse_response: Any = None
        self._sse_lines: Any = None
        self.server_capabilities: dict[str, Any] = {}
