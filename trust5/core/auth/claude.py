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
