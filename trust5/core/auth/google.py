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
