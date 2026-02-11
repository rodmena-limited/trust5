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

class TokenStore:
    def __init__(self, base_dir: str | None = None):
        if base_dir:
            self._dir = Path(base_dir)
        else:
            self._dir = Path.home() / _TRUST5_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(self._load_or_create_key())

    def _load_or_create_key(self) -> bytes:
        key_path = self._dir / _KEY_FILE
        if key_path.exists():
            return key_path.read_bytes()
        key = Fernet.generate_key()
        key_path.write_bytes(key)
        key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return key
