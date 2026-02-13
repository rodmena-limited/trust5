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
