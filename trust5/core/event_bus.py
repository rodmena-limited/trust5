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
