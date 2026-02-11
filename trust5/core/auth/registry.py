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
