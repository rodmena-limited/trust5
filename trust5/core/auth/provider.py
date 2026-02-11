from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class TokenData:
    access_token: str
    refresh_token: str | None = None
    expires_at: float = 0.0
    token_type: str = "Bearer"
    scopes: list[str] = field(default_factory=list)
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        if self.expires_at <= 0:
            return False
        return time.time() >= self.expires_at

    @property
    def expires_in_seconds(self) -> float:
        if self.expires_at <= 0:
            return float("inf")
        return max(0.0, self.expires_at - time.time())

    @property
    def should_refresh(self) -> bool:
        return self.refresh_token is not None and self.expires_in_seconds < 300


@dataclass
class ProviderConfig:
    name: str
    display_name: str
    api_base_url: str
    auth_header: str = "x-api-key"
    backend: str = "openai"
    models: dict[str, str] = field(default_factory=dict)
    thinking_tiers: set[str] = field(default_factory=set)
    fallback_chain: list[str] = field(default_factory=list)


class AuthProvider(ABC):
    def __init__(self, config: ProviderConfig):
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @abstractmethod
    def login(self) -> TokenData: ...

    @abstractmethod
    def refresh(self, token_data: TokenData) -> TokenData: ...

    @abstractmethod
    def validate(self, token_data: TokenData) -> bool: ...

    def logout_cleanup(self) -> None:
        pass
