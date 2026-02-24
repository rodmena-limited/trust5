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
        "watchdog": "claude-haiku-4-5",
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
    """Anthropic Claude OAuth provider with PKCE authorization flow."""

    def __init__(self) -> None:
        super().__init__(CLAUDE_CONFIG)

    def login(self) -> TokenData:
        verifier, challenge = _generate_pkce()
        state = secrets.token_urlsafe(32)

        params = {
            "code": "true",
            "client_id": _CLIENT_ID,
            "response_type": "code",
            "redirect_uri": _REDIRECT_URI,
            "scope": _SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        auth_url = f"{_AUTH_URL}?{urlencode(params)}"

        print("\nOpening browser for Claude Max authorization...\n")  # User-facing CLI output
        print(f"If the browser does not open, visit:\n{auth_url}\n")  # User-facing CLI output
        webbrowser.open(auth_url, new=2)

        # User-facing CLI output
        print("After authorizing, you will see a code in the browser.\nPaste the full response (code#state) below:\n")
        raw_response = input("Authorization response: ").strip()

        if "#" in raw_response:
            auth_code, returned_state = raw_response.split("#", 1)
        else:
            auth_code = raw_response
            returned_state = state

        return self._exchange_code(auth_code, returned_state, verifier)

    def _exchange_code(self, code: str, state: str, verifier: str) -> TokenData:
        resp = requests.post(
            _TOKEN_URL,
            json={
                "code": code,
                "state": state,
                "grant_type": "authorization_code",
                "client_id": _CLIENT_ID,
                "redirect_uri": _REDIRECT_URI,
                "code_verifier": verifier,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        return TokenData(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=time.time() + data.get("expires_in", _TOKEN_EXPIRY),
            token_type=data.get("token_type", "Bearer"),
            scopes=[str(s) for s in _SCOPES.split()],
        )

    def refresh(self, token_data: TokenData) -> TokenData:
        if not token_data.refresh_token:
            raise ValueError("No refresh token available")

        resp = requests.post(
            _TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "refresh_token": token_data.refresh_token,
                "client_id": _CLIENT_ID,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        return TokenData(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", token_data.refresh_token),
            expires_at=time.time() + data.get("expires_in", _TOKEN_EXPIRY),
            token_type=data.get("token_type", "Bearer"),
            scopes=token_data.scopes,
        )

    def validate(self, token_data: TokenData) -> bool:
        try:
            resp = requests.post(
                f"{self.config.api_base_url}/v1/messages",
                headers={
                    "Authorization": f"Bearer {token_data.access_token}",
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta": "oauth-2025-04-20",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-5",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                timeout=10,
            )
            return resp.status_code == 200
        except requests.RequestException:
            return False
