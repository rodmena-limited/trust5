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
