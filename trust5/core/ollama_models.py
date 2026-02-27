"""Ensure required Ollama models are available before pipeline execution.

When Trust5 runs with ``--provider ollama``, this module checks which models
are configured in ``~/.trust5/config.yaml`` (``models.ollama`` section) and
pulls any that are missing from the local Ollama instance.

Called once per process (idempotent via ``_models_checked`` flag) before the
first LLM call.  All errors emit warnings — the pipeline always attempts to
run even if pulling fails.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

import requests

from .auth.registry import get_active_token
from .config import load_global_config
from .message import M, emit

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_HEALTH_TIMEOUT = 5  # seconds for health/list HTTP requests
OLLAMA_PULL_TIMEOUT_CLOUD = 120  # 2 min for :cloud models (lightweight descriptor)
OLLAMA_PULL_TIMEOUT_LOCAL = 600  # 10 min for local models (weight download)

# ── Idempotency guard ─────────────────────────────────────────────────────────

_models_checked = False


# ── Helpers ───────────────────────────────────────────────────────────────────


def _is_cloud_model(name: str) -> bool:
    """Return True if model name has a ``:cloud`` tag (fast pull, no weights)."""
    return ":cloud" in name


def _normalize_model_name(name: str) -> str:
    """Add ``:latest`` tag if no tag is present.

    Ollama lists ``llama3`` as ``llama3:latest``.  Normalizing ensures
    config names without explicit tags match the server listing.
    """
    if ":" not in name:
        return f"{name}:latest"
    return name


def _ollama_available() -> bool:
    """Check if the ``ollama`` CLI binary is on PATH."""
    return shutil.which("ollama") is not None


def _ollama_server_running() -> bool:
    """Check if the Ollama server at ``localhost:11434`` is responding."""
    try:
        resp = requests.get(
            f"{OLLAMA_BASE_URL}/api/tags",
            timeout=OLLAMA_HEALTH_TIMEOUT,
        )
        return resp.status_code == 200
    except (requests.ConnectionError, requests.Timeout):
        return False


def _list_available_models() -> set[str]:
    """Return set of locally available model names from the Ollama server.

    Names are normalized (bare names get ``:latest`` appended) so they
    can be compared directly against config-derived names.
    """
    try:
        resp = requests.get(
            f"{OLLAMA_BASE_URL}/api/tags",
            timeout=OLLAMA_HEALTH_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return {_normalize_model_name(m["name"]) for m in data.get("models", [])}
    except (requests.RequestException, KeyError, ValueError):
        return set()


def _get_required_models() -> set[str]:
    """Collect all unique model names from ``GlobalConfig.models.ollama``.

    Reads every tier (best, good, fast, watchdog, default) plus the full
    ``fallback_chain``.  Names are normalized for consistent matching.
    """
    gcfg = load_global_config()
    ollama_cfg = gcfg.models.ollama
    models: set[str] = set()
    for tier in ("best", "good", "fast", "watchdog", "default"):
        name = getattr(ollama_cfg, tier, None)
        if name:
            models.add(_normalize_model_name(name))
    for name in ollama_cfg.fallback_chain:
        models.add(_normalize_model_name(name))
    return models


def _pull_model(name: str) -> bool:
    """Pull a single Ollama model via ``ollama pull <name>``.

    Returns ``True`` on success (exit code 0), ``False`` on failure or timeout.
    Cloud models (``:cloud``) use a shorter timeout since they only register a
    reference (no weight download).
    """
    timeout = OLLAMA_PULL_TIMEOUT_CLOUD if _is_cloud_model(name) else OLLAMA_PULL_TIMEOUT_LOCAL
    try:
        proc = subprocess.run(
            ["ollama", "pull", name],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            stderr_snippet = proc.stderr.strip()[:200] if proc.stderr else ""
            logger.warning("ollama pull %s failed (rc=%d): %s", name, proc.returncode, stderr_snippet)
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        logger.warning("ollama pull %s timed out after %ds", name, timeout)
        return False
    except OSError as e:
        logger.warning("ollama pull %s OS error: %s", name, e)
        return False


# ── Main entry point ──────────────────────────────────────────────────────────


def ensure_ollama_models() -> None:
    """Check and pull missing Ollama models before pipeline execution.

    Called once per process from ``main.py`` before the first LLM call
    (in ``develop``, ``plan``, ``run``, ``loop`` commands).

    Provider gate: immediately returns if the active provider is not Ollama.
    Idempotency: guarded by ``_models_checked`` module flag.
    Error handling: all failures emit warnings — the pipeline always proceeds.
    """
    global _models_checked  # noqa: PLW0603
    if _models_checked:
        return
    _models_checked = True

    if get_active_token() is not None:
        return  # Using Claude/Google — no model pulling needed

    # Pre-flight: ollama CLI must be installed
    if not _ollama_available():
        emit(M.SWRN, "Ollama CLI not found in PATH — skipping model availability check")
        return

    # Pre-flight: Ollama server must be reachable
    if not _ollama_server_running():
        emit(M.SWRN, f"Ollama server not responding at {OLLAMA_BASE_URL} — skipping model availability check")
        return

    # Determine what's needed vs. what's available
    required = _get_required_models()
    if not required:
        return

    available = _list_available_models()
    missing = required - available

    if not missing:
        emit(M.SINF, f"All {len(required)} Ollama model(s) available")
        return

    # Pull missing models
    emit(M.SINF, f"Pulling {len(missing)} missing Ollama model(s): {', '.join(sorted(missing))}")

    failed: list[str] = []
    for i, name in enumerate(sorted(missing), 1):
        emit(M.SINF, f"  [{i}/{len(missing)}] Pulling {name}...")
        if _pull_model(name):
            emit(M.SINF, f"  [{i}/{len(missing)}] {name} — OK")
        else:
            emit(M.SWRN, f"  [{i}/{len(missing)}] {name} — FAILED")
            failed.append(name)

    if failed:
        emit(
            M.SWRN,
            f"Failed to pull {len(failed)} model(s): {', '.join(failed)}. "
            f"Pipeline will attempt to run — LLM fallback chains may recover.",
        )
    else:
        emit(M.SINF, f"All {len(missing)} model(s) pulled successfully")
