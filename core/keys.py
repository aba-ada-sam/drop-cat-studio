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
    val = creds.get(config_key, "") or creds.get(f"{provider}_api_key", "")
    if not val and provider == "openai":
        val = creds.get("open_ai_key", "")
    return val


def save_keys(**kwargs):
    """Persist API keys to config. Keys: anthropic_key, openai_key."""
    updates = {}
    for provider in ("anthropic", "openai"):
        key_name = f"{provider}_key"
        if key_name in kwargs and kwargs[key_name]:
            updates[key_name] = kwargs[key_name]
    if updates:
        cfg.save(updates)


# -- Uncensored backend helpers (Featherless cloud / KoboldCpp local) ---------
# Ollama was removed 2026-07-05; the uncensored LLM/vision role is now served by
# Featherless (default) or a local KoboldCpp. See core/llm_client.py.

def get_featherless_key() -> str:
    """Return the Featherless API key from config or the credentials key file."""
    val = cfg.get("featherless_key") or ""
    if val:
        return val
    key_file = cfg.get("featherless_key_file") or r"C:\JSON Credentials\featherless_api_key.txt"
    try:
        p = Path(key_file)
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def uncensored_provider() -> str:
    """Which uncensored backend is preferred: 'featherless' (cloud) or 'kobold'."""
    return cfg.get("uncensored_provider") or "featherless"


def list_uncensored_models() -> list[str]:
    """List models from the active uncensored backend.

    For Featherless this catalog is thousands of entries, which is useless for a
    picker and expensive to fetch -- the model is a free-text field, so we return
    []. For a local KoboldCpp it's the single loaded model (cheap + useful).
    """
    if uncensored_provider() != "kobold":
        return []
    try:
        from core.llm_client import LLMClient
        return LLMClient().list_models("kobold")
    except Exception:
        return []


def status() -> dict:
    """Availability of the uncensored LLM backend (for the settings/status UI).

    Lightweight: does NOT enumerate the (huge) Featherless catalog -- /api/system
    polls this every 15s, so availability is just "key present" / "server up".
    """
    prov = uncensored_provider()
    try:
        from core.llm_client import LLMClient
        available = LLMClient().is_available(prov)
    except Exception:
        available = False
    return {"available": available, "provider": prov,
            "models": list_uncensored_models() if (available and prov == "kobold") else []}
