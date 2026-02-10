from __future__ import annotations
import base64
import hashlib
import json
import logging
import os
import secrets
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlencode
import requests
from .callback import run_callback_server
from .provider import AuthProvider, ProviderConfig, TokenData
logger = logging.getLogger(__name__)
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_REDIRECT_URI = "http://localhost:8585"
_SCOPES = "https://www.googleapis.com/auth/generative-language.tuning"
_TOKEN_EXPIRY = 3600
GOOGLE_CONFIG = ProviderConfig(
    name="google",
    display_name="Google Gemini",
    api_base_url="https://generativelanguage.googleapis.com",
    auth_header="Authorization",
    backend="google",
    models={
        "best": "gemini-3-pro-preview",
        "good": "gemini-3-pro-preview",
        "fast": "gemini-3-flash-preview",
        "default": "gemini-3-pro-preview",
    },
    thinking_tiers={"best", "good"},
    fallback_chain=[
        "gemini-3-pro-preview",
        "gemini-3-flash-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
    ],
)

def _generate_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge

def _load_client_json(path: str) -> tuple[str, str]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    for key in ("installed", "web"):
        if key in data:
            return data[key]["client_id"], data[key]["client_secret"]
    raise ValueError(f"No 'installed' or 'web' key in {path}")

class GoogleProvider(AuthProvider):
    def __init__(self) -> None:
        super().__init__(GOOGLE_CONFIG)

    def _resolve_credentials() -> tuple[str, str]:
        cid = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
        csec = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
        if cid and csec:
            return cid, csec

        json_path = os.environ.get("GOOGLE_CLIENT_SECRET_FILE", "").strip()
        if json_path and Path(json_path).exists():
            return _load_client_json(json_path)

        home_path = Path.home() / ".trust5" / "google_client.json"
        if home_path.exists():
            return _load_client_json(str(home_path))

        print("\nGoogle Gemini OAuth Login")
        print("=" * 40)
        print(
            "\nYou need a Google Cloud OAuth 2.0 Desktop Client.\n"
            "Create one at: https://console.cloud.google.com/apis/credentials\n"
            "Enable the Generative Language API first:\n"
            "  https://console.cloud.google.com/apis/library/generativelanguage.googleapis.com\n"
            "\nOptions:\n"
            "  1) Place client_secret*.json at ~/.trust5/google_client.json\n"
            "  2) Set GOOGLE_CLIENT_SECRET_FILE=/path/to/client_secret.json\n"
            "  3) Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET env vars\n"
            "  4) Enter credentials manually below\n"
        )
        cid = input("OAuth Client ID: ").strip()
        csec = input("OAuth Client Secret: ").strip()
        return cid, csec

    def login(self) -> TokenData:
        client_id, client_secret = self._resolve_credentials()
        if not client_id or not client_secret:
            raise ValueError("Client ID and Client Secret are required")

        verifier, challenge = _generate_pkce()
        state = secrets.token_urlsafe(32)

        params = {
            "client_id": client_id,
            "redirect_uri": _REDIRECT_URI,
            "response_type": "code",
            "scope": _SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
        auth_url = f"{_AUTH_URL}?{urlencode(params)}"

        print("\nOpening browser for Google authorization...\n")
        print(f"If the browser does not open, visit:\n{auth_url}\n")
        webbrowser.open(auth_url, new=2)

        code, error = run_callback_server(port=8585, timeout=120)

        if error:
            raise ValueError(f"Authorization denied: {error}")
        if not code:
            raise ValueError("No authorization code received (timeout?)")

        return self._exchange_code(code, verifier, client_id, client_secret)
