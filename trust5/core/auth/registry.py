from __future__ import annotations
import logging
from collections.abc import Callable
from .claude import ClaudeProvider
from .google import GoogleProvider
from .provider import AuthProvider, ProviderConfig, TokenData
from .token_store import TokenStore
logger = logging.getLogger(__name__)
_PROVIDERS: dict[str, Callable[[], AuthProvider]] = {
    "claude": ClaudeProvider,
    "google": GoogleProvider,
}
_store_instance: TokenStore | None = None
_provider_override: str | None = None
DEFAULT_PROVIDER = "claude"

def _get_store() -> TokenStore:
    global _store_instance
    if _store_instance is None:
        _store_instance = TokenStore()
    return _store_instance
