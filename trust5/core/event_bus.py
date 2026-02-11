from __future__ import annotations
import collections
import json
import logging
import os
import queue
import socket
import threading
from dataclasses import dataclass
logger = logging.getLogger(__name__)
K_MSG = "msg"  # single-line message
K_BLOCK_START = "bs"  # block start (label)
K_BLOCK_LINE = "bl"  # block line (content)
K_BLOCK_END = "be"  # block end
K_STREAM_START = "ss"  # stream start (label)
K_STREAM_TOKEN = "st"  # stream token
K_STREAM_END = "se"  # stream end
_SENTINEL: Event | None = None
_MAX_QUEUE = 10_000
_REPLAY_BUFFER_SIZE = 100  # Keep last N events for replay to new subscribers
_bus: EventBus | None = None
_bus_lock = threading.Lock()

@dataclass(frozen=True)
class Event:
    """Immutable event emitted by the pipeline."""
    kind: str
    code: str
    ts: str
    msg: str = ''
    label: str = ''

    def to_json(self) -> str:
        d: dict[str, str] = {"k": self.kind, "c": self.code, "t": self.ts}
        if self.msg:
            d["m"] = self.msg
        if self.label:
            d["l"] = self.label
        return json.dumps(d, ensure_ascii=False)
