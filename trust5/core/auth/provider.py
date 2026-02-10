from __future__ import annotations
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

@dataclass
class TokenData:
    access_token: str
    refresh_token: str | None = None
    expires_at: float = 0.0
    token_type: str = 'Bearer'
    scopes: list[str] = field(default_factory=list)
    extra: dict[str, str] = field(default_factory=dict)

    def is_expired(self) -> bool:
        if self.expires_at <= 0:
            return False
        return time.time() >= self.expires_at
