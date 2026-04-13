"""API key management for Drop Cat Go Studio.

Key loading precedence (highest wins):
  1. config.json in project root (set via Settings UI)
  2. C:/JSON Credentials/QB_WC_credentials.json (local fallback)
If a key exists in both, the config.json value is used.
"""
import json
import logging
from pathlib import Path

from core import config as cfg

log = logging.getLogger(__name__)

_CREDS_FILE = Path("C:/JSON Credentials/QB_WC_credentials.json")


def _load_creds() -> dict:
    """Load the local credentials file if it exists."""
    try:
        if _CREDS_FILE.exists():
            return json.loads(_CREDS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def get_key(provider: str) -> str:
    """Return the API key for 'anthropic' or 'openai'. Empty string if not set."""
    config_key = f"{provider}_key"
    val = cfg.get(config_key) or ""
    if val:
        return val
    # Fall back to local credentials file
    creds = _load_creds()
    return creds.get(config_key, "") or creds.get(f"{provider}_api_key", "")


def save_keys(**kwargs):
    """Persist API keys to config. Keys: anthropic_key, openai_key."""
    updates = {}
    for provider in ("anthropic", "openai"):
        key_name = f"{provider}_key"
        if key_name in kwargs and kwargs[key_name]:
            updates[key_name] = kwargs[key_name]
    if updates:
        cfg.save(updates)


# ── Ollama helpers (unchanged) ────────────────────────────────────────────────

def get_ollama_host() -> str:
    return cfg.get("ollama_host") or "http://localhost:11434"


def get_ollama_models() -> list[str]:
    try:
        import ollama
        client = ollama.Client(host=get_ollama_host())
        return [m.model for m in client.list().models]
    except Exception:
        return []


def status() -> dict:
    try:
        import ollama
        client = ollama.Client(host=get_ollama_host())
        models = [m.model for m in client.list().models]
        return {"available": True, "models": models}
    except Exception:
        return {"available": False, "models": []}
