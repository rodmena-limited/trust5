from __future__ import annotations

import logging
import threading
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
_registry_lock = threading.Lock()


def _get_store() -> TokenStore:
    global _store_instance
    with _registry_lock:
        if _store_instance is None:
            _store_instance = TokenStore()
        return _store_instance


def set_provider_override(name: str | None) -> None:
    global _provider_override
    with _registry_lock:
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


def get_active_token() -> tuple[AuthProvider, TokenData] | None:
    store = _get_store()
    active_name = _provider_override or store.get_active() or DEFAULT_PROVIDER
    if active_name == "ollama":
        return None

    try:
        provider = get_provider(active_name)
    except ValueError:
        return None

    token_data = store.get_valid_token(active_name, provider)
    if token_data is None:
        return None

    return provider, token_data


DEFAULT_PROVIDER = "claude"


def do_login(provider_name: str) -> TokenData:
    provider = get_provider(provider_name)
    token_data = provider.login()

    store = _get_store()
    store.save(provider_name, token_data)

    if store.get_active() is None or store.get_active() == "":
        store.set_active(provider_name)

    logger.info("Logged in to %s", provider.config.display_name)
    return token_data


def do_logout(provider_name: str | None = None) -> bool:
    store = _get_store()

    if provider_name is None:
        provider_name = store.get_active()

    if provider_name is None:
        return False

    try:
        provider = get_provider(provider_name)
        provider.logout_cleanup()
    except ValueError:
        pass

    deleted = store.delete(provider_name)

    if store.get_active() == provider_name:
        store.set_active("")

    return deleted
