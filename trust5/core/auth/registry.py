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

def set_provider_override(name: str | None) -> None:
    global _provider_override
    _provider_override = name

def register_provider(name: str, factory: Callable[[], AuthProvider]) -> None:
    _PROVIDERS[name] = factory

def list_providers() -> list[str]:
    return list(_PROVIDERS.keys())

def get_provider(name: str) -> AuthProvider:
    cls = _PROVIDERS.get(name)
    if cls is None:
        available = ", ".join(_PROVIDERS.keys())
        raise ValueError(f"Unknown provider '{name}'. Available: {available}")
    return cls()

def get_provider_config(name: str) -> ProviderConfig:
    return get_provider(name).config

def get_active_provider() -> AuthProvider | None:
    store = _get_store()
    active = store.get_active()
    if active is None:
        return None
    try:
        return get_provider(active)
    except ValueError:
        return None
