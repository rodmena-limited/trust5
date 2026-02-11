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

    def save(self, provider_name: str, token_data: TokenData) -> None:
        all_tokens = self._load_all()
        all_tokens[provider_name] = {
            "access_token": token_data.access_token,
            "refresh_token": token_data.refresh_token,
            "expires_at": token_data.expires_at,
            "token_type": token_data.token_type,
            "scopes": token_data.scopes,
            "extra": token_data.extra,
        }
        self._save_all(all_tokens)

    def load(self, provider_name: str) -> TokenData | None:
        all_tokens = self._load_all()
        data = all_tokens.get(provider_name)
        if data is None:
            return None
        return TokenData(
            access_token=str(data["access_token"]),
            refresh_token=str(data["refresh_token"]) if data.get("refresh_token") else None,
            expires_at=float(data.get("expires_at", 0.0)),
            token_type=str(data.get("token_type", "Bearer")),
            scopes=list(data.get("scopes", [])),
            extra=dict(data.get("extra", {})),
        )

    def delete(self, provider_name: str) -> bool:
        all_tokens = self._load_all()
        if provider_name not in all_tokens:
            return False
        del all_tokens[provider_name]
        self._save_all(all_tokens)
        return True

    def list_providers(self) -> list[str]:
        return list(self._load_all().keys())
