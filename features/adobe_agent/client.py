"""HTTP client for the CEP panel servers running inside Premiere and AE."""
import logging
import requests

log = logging.getLogger(__name__)

PREMIERE_PORT = 7920
AE_PORT       = 7921


def health(port: int) -> dict | None:
    try:
        r = requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
        return r.json() if r.ok else None
    except Exception:
        return None


def run_op(port: int, op: str, args: dict = None) -> dict:
    """Execute one operation. Raises RuntimeError on failure."""
    payload = {"op": op, "args": args or {}}
    log.info("[adobe] port=%d op=%s args=%s", port, op, args)
    try:
        r = requests.post(f"http://127.0.0.1:{port}/run", json=payload, timeout=30)
        r.raise_for_status()
        result = r.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Panel not responding on port {port}: {exc}") from exc
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "unknown panel error"))
    return result.get("data") or {}


def status_both() -> dict:
    pr = health(PREMIERE_PORT)
    ae = health(AE_PORT)
    return {
        "premiere": {"connected": pr is not None, "info": pr},
        "aftereffects": {"connected": ae is not None, "info": ae},
    }
