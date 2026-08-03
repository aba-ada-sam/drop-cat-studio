"""Local-vs-RunPod dispatch for a Forge txt2img payload.

Single choke point so Image Studio and Chat Studio don't each hand-roll the
local/cloud branch. Callers pass an already-built Forge payload and get back
the parsed JSON response body either way.
"""
import requests

from core import config as cfg
from core import runpod_forge

FORGE_TXT2IMG_URL = "http://127.0.0.1:7861/sdapi/v1/txt2img"


class ForgeDispatchError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def txt2img(payload: dict, timeout: float = 600.0) -> dict:
    """POST a Forge txt2img payload to whichever backend is configured
    (image_gpu_backend: 'local' | 'runpod'). Blocking -- call via
    asyncio.to_thread from an async route. Raises ForgeDispatchError."""
    backend = (cfg.get("image_gpu_backend", "local") or "local").strip().lower()

    if backend == "runpod":
        try:
            return runpod_forge.txt2img(payload, timeout=timeout)
        except runpod_forge.RunPodError as e:
            raise ForgeDispatchError(str(e), 502)

    try:
        r = requests.post(FORGE_TXT2IMG_URL, json=payload, timeout=timeout)
    except requests.exceptions.ConnectionError:
        raise ForgeDispatchError("Forge is not running on :7861 -- start it from its own GUI", 503)
    except requests.exceptions.RequestException as e:
        raise ForgeDispatchError(f"Forge request failed: {e}", 502)
    if not r.ok:
        raise ForgeDispatchError(f"Forge returned {r.status_code}: {r.text[:200]}", 502)
    try:
        return r.json()
    except Exception as e:
        raise ForgeDispatchError(f"Forge response unreadable: {e}", 502)
