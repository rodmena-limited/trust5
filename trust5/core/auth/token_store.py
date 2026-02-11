from __future__ import annotations
import json
import logging
import stat
from pathlib import Path
from typing import Any
from cryptography.fernet import Fernet, InvalidToken  # pyright: ignore[reportMissingImports]
from .provider import AuthProvider, TokenData
logger = logging.getLogger(__name__)
_TRUST5_DIR = ".trust5"
_KEY_FILE = "auth.key"
_TOKEN_FILE = "tokens.enc"
