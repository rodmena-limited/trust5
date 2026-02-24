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
    """Encrypted credential store using Fernet symmetric encryption."""

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

    def get_valid_token(self, provider_name: str, provider: AuthProvider) -> TokenData | None:
        token_data = self.load(provider_name)
        if token_data is None:
            return None

        if token_data.should_refresh:
            try:
                token_data = provider.refresh(token_data)
                self.save(provider_name, token_data)
                logger.info("Token refreshed for provider %s", provider_name)
            except (OSError, ValueError, RuntimeError):  # refresh: network/auth errors
                logger.warning(
                    "Token refresh failed for %s, using existing token",
                    provider_name,
                )

        if token_data.is_expired:
            logger.warning("Token expired for %s", provider_name)
            return None

        return token_data

    def set_active(self, provider_name: str) -> None:
        meta = self._load_meta()
        meta["active_provider"] = provider_name
        self._save_meta(meta)

    def get_active(self) -> str | None:
        meta = self._load_meta()
        return meta.get("active_provider")

    def _token_path(self) -> Path:
        return self._dir / _TOKEN_FILE

    def _meta_path(self) -> Path:
        return self._dir / "auth_meta.json"

    def _load_all(self) -> dict[str, dict[str, Any]]:
        path = self._token_path()
        if not path.exists():
            return {}
        try:
            decrypted = self._fernet.decrypt(path.read_bytes())
            result: dict[str, dict[str, Any]] = json.loads(decrypted)
            return result
        except (InvalidToken, json.JSONDecodeError):
            logger.warning("Corrupted token store, starting fresh")
            return {}

    def _save_all(self, data: dict[str, dict[str, Any]]) -> None:
        path = self._token_path()
        encrypted = self._fernet.encrypt(json.dumps(data).encode())
        path.write_bytes(encrypted)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def _load_meta(self) -> dict[str, Any]:
        path = self._meta_path()
        if not path.exists():
            return {}
        try:
            result: dict[str, Any] = json.loads(path.read_text())
            return result
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_meta(self, data: dict[str, Any]) -> None:
        path = self._meta_path()
        path.write_text(json.dumps(data))
