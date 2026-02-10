from __future__ import annotations
import base64
import hashlib
import logging
import secrets
import time
import webbrowser
from urllib.parse import urlencode
import requests
from .provider import AuthProvider, ProviderConfig, TokenData
logger = logging.getLogger(__name__)
_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_AUTH_URL = "https://claude.ai/oauth/authorize"
_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
_SCOPES = "org:create_api_key user:profile user:inference"
_TOKEN_EXPIRY = 28800  # 8 hours
CLAUDE_CONFIG = ProviderConfig(
    name="claude",
    display_name="Claude Max",
    api_base_url="https://api.anthropic.com",
    auth_header="Authorization",
    backend="anthropic",
    models={
        "best": "claude-opus-4-6",
        "good": "claude-opus-4-6",
        "fast": "claude-sonnet-4-5",
        "default": "claude-opus-4-6",
    },
    thinking_tiers={"best", "good"},
    fallback_chain=[
        "claude-opus-4-6",
        "claude-sonnet-4-5",
        "claude-haiku-4-5",
    ],
)

def _generate_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge

class ClaudeProvider(AuthProvider):
    def __init__(self) -> None:
        super().__init__(CLAUDE_CONFIG)
