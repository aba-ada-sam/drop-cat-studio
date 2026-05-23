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


# -- Ollama helpers ------------------------------------------------

def _split_url(url: str) -> tuple[str, str, int, str]:
    """Crude URL splitter: returns (scheme, host, port, path)."""
    from urllib.parse import urlparse
    p = urlparse(url)
    scheme = p.scheme or "http"
    host = p.hostname or "localhost"
    port = p.port or (443 if scheme == "https" else 80)
    return scheme, host, port, (p.path or "")


def get_ollama_host() -> str:
    """Return the Ollama base URL. Auto-discovers on LAN when the configured
    host is remote and unreachable (only when auto_discover_satellite is True)."""
    raw = cfg.get("ollama_host") or "http://localhost:11434"
    try:
        scheme, host, port, _ = _split_url(raw)
    except Exception:
        return raw
    if host.lower() in ("localhost", "127.0.0.1", "0.0.0.0"):
        return raw
    if not cfg.get("auto_discover_satellite"):
        return raw
    try:
        from core import satellite_discovery as _disc
        found = _disc.discover(
            port=port,
            health_path="/api/tags",
            cached_host=host,
            hostname_hints=cfg.get("satellite_hostnames"),
            log_label="ollama",
        )
    except Exception:
        return raw
    if not found or found == host:
        return raw
    new_url = f"{scheme}://{found}:{port}"
    try:
        cfg.set_val("ollama_host", new_url)
    except Exception:
        pass
    return new_url


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
