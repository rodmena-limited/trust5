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

    @property
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
                logger.debug("Failed to terminate MCP server %s", self.name, exc_info=True)
                if self.process.poll() is None:
                    try:
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

    @property
    def is_running(self) -> bool:
        return self._message_url is not None

    def start(self) -> None:
        import requests

        self._session = requests.Session()
        self._sse_response = self._session.get(
            self.url,
            stream=True,
            headers={"Accept": "text/event-stream"},
            timeout=self.timeout,
        )
        self._sse_response.raise_for_status()
        self._sse_lines = self._sse_response.iter_lines(decode_unicode=True)

        # Read SSE stream until we get the 'endpoint' event
        endpoint = self._read_sse_event("endpoint")
        if not endpoint:
            raise RuntimeError(f"MCP SSE server '{self.name}' didn't send endpoint event")

        # Build full URL for POSTing JSON-RPC messages
        base = self.url.rsplit("/", 1)[0]
        if endpoint.startswith("http"):
            self._message_url = endpoint
        elif endpoint.startswith("/"):
            self._message_url = base + endpoint
        else:
            self._message_url = base + "/" + endpoint

        # MCP initialize handshake
        resp = self._send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "trust5", "version": "0.1.0"},
            },
        )
        self.server_capabilities = resp.get("result", {}).get("capabilities", {})
        self._send_notification("notifications/initialized")

    def stop(self) -> None:
        if self._sse_response:
            try:
                self._sse_response.close()
            except Exception:
                logger.debug("Failed to close SSE response for %s", self.name, exc_info=True)
        if self._session:
            try:
                self._session.close()
            except Exception:
                logger.debug("Failed to close SSE session for %s", self.name, exc_info=True)
        self._sse_response = None
        self._session = None
        self._message_url = None
        self._sse_lines = None

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
        msg_id = self.msg_id

        req: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            req["params"] = params

        # POST the JSON-RPC request
        resp = self._session.post(
            self._message_url,
            json=req,
            timeout=self.timeout,
        )
        resp.raise_for_status()

        # Read SSE stream until we get the matching response
        return self._read_sse_response(msg_id)

    def _send_notification(self, method: str, params: Any = None) -> None:
        req: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            req["params"] = params

        self._session.post(
            self._message_url,
            json=req,
            timeout=self.timeout,
        )

    def _read_sse_event(self, target_type: str) -> str | None:
        """Read SSE stream until an event of `target_type` arrives."""
        event_type = ""
        data_lines: list[str] = []

        for line in self._sse_lines:
            if line is None:
                continue
            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")

            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif line == "":
                # Empty line = end of SSE event
                if data_lines and event_type == target_type:
                    return "\n".join(data_lines)
                event_type = ""
                data_lines = []

        return None

    def _read_sse_response(self, msg_id: int) -> dict[str, Any]:
        """Read SSE stream until a JSON-RPC response with matching id arrives."""
        event_type = ""
        data_lines: list[str] = []

        for line in self._sse_lines:
            if line is None:
                continue
            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")

            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif line == "":
                if data_lines and event_type == "message":
                    data = "\n".join(data_lines)
                    try:
                        msg: dict[str, Any] = json.loads(data)
                        if msg.get("id") == msg_id:
                            return msg
                    except json.JSONDecodeError:
                        logger.debug("Invalid JSON in SSE from '%s'", self.name)
                event_type = ""
                data_lines = []

        raise RuntimeError(f"MCP SSE server '{self.name}' stream ended without response")
