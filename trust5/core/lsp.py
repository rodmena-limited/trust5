import json
import logging
import os
import queue
import subprocess
import threading
import time
from typing import Any

class JsonRpcClient:
    def __init__(self, command: list[str], cwd: str = "."):
        self.command = command
        self.cwd = cwd
        self.process: subprocess.Popen[bytes] | None = None
        self.msg_id = 0
        self.responses: dict[int, Any] = {}
        self.notifications: queue.Queue[dict[str, Any]] = queue.Queue()
        self.running = False
        self._lock = threading.Lock()

    def start(self) -> None:
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd,
            text=False,
        )
        self.running = True
        threading.Thread(target=self._read_loop, daemon=True).start()

    def stop(self) -> None:
        self.running = False
        if self.process:
            self.process.terminate()
            self.process = None

    def send_request(self, method: str, params: Any = None) -> Any:
        with self._lock:
            self.msg_id += 1
            mid = self.msg_id

        req = {"jsonrpc": "2.0", "id": mid, "method": method, "params": params}
        self._send(req)

        start_time = time.time()
        while time.time() - start_time < 10:
            if mid in self.responses:
                return self.responses.pop(mid)
            time.sleep(0.01)

        raise TimeoutError(f"RPC Timeout for {method}")

    def send_notification(self, method: str, params: Any = None) -> None:
        req: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        self._send(req)

    def _send(self, msg: dict[str, Any]) -> None:
        content = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(content)}\r\n\r\n".encode()
        if self.process and self.process.stdin:
            self.process.stdin.write(header + content)
            self.process.stdin.flush()

    def _read_loop(self) -> None:
        buffer = b""
        while self.running and self.process:
            while b"\r\n\r\n" not in buffer:
                if self.process.stdout is None:
                    self.running = False
                    return
                chunk = self.process.stdout.read(1)
                if not chunk:
                    self.running = False
                    return
                buffer += chunk

            header_part, body_part = buffer.split(b"\r\n\r\n", 1)
            buffer = body_part

            content_len = 0
            for line in header_part.decode("utf-8").split("\r\n"):
                if line.startswith("Content-Length:"):
                    content_len = int(line.split(":")[1].strip())

            while len(buffer) < content_len:
                if self.process.stdout is None:
                    self.running = False
                    return
                chunk = self.process.stdout.read(content_len - len(buffer))
                if not chunk:
                    self.running = False
                    return
                buffer += chunk

            msg_bytes = buffer[:content_len]
            buffer = buffer[content_len:]

            try:
                msg = json.loads(msg_bytes.decode("utf-8"))
                if "id" in msg:
                    self.responses[msg["id"]] = msg.get("result") or msg.get("error")
                else:
                    self.notifications.put(msg)
            except Exception as e:
                logging.getLogger(__name__).debug("JSON Parse Error: %s", e)
