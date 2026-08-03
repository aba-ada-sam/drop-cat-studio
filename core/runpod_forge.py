"""RunPod dispatch for Forge image generation.

The cloud twin of a direct POST to Forge's own sdapi/v1/txt2img: wraps the
same payload for RunPod's serverless job API (submit -> poll -> completed),
targeting the dcg-tiles endpoint (perfection25D, same NSFW capability as the
local checkpoint -- see project memory). Returns a dict shaped identically to
Forge's own sync response ({"images": [...], "info": "..."}) so callers don't
need to branch on backend once they call txt2img().
"""
import json
import time
from pathlib import Path

import requests

from core import config as cfg

RUNPOD_BASE = "https://api.runpod.ai/v2"
POLL_INTERVAL = 2.0


class RunPodError(Exception):
    pass


def _creds() -> tuple[str, str]:
    """Return (api_key, endpoint_id), preferring explicit config over the
    shared credentials file."""
    config = cfg.load()
    api_key = (config.get("runpod_api_key") or "").strip()
    endpoint_id = (config.get("runpod_image_endpoint_id") or "").strip()
    if api_key and endpoint_id:
        return api_key, endpoint_id

    key_file = (config.get("runpod_key_file") or "").strip()
    if key_file and Path(key_file).exists():
        try:
            data = json.loads(Path(key_file).read_text(encoding="utf-8"))
            api_key = api_key or (data.get("api_key") or "").strip()
            endpoint_id = endpoint_id or (data.get("endpoint_id") or "").strip()
        except Exception:
            pass
    return api_key, endpoint_id


def txt2img(payload: dict, timeout: float = 600.0) -> dict:
    """Run a Forge txt2img payload on the configured RunPod endpoint.

    Raises RunPodError on any failure (no key/endpoint, submit failure, job
    failure, timeout).
    """
    api_key, endpoint_id = _creds()
    if not endpoint_id:
        raise RunPodError("No RunPod image endpoint configured -- set it in Settings")
    if not api_key:
        raise RunPodError("No RunPod API key configured -- set it in Settings or C:\\JSON Credentials\\runpod.json")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"input": {"api": {"method": "POST", "endpoint": "sdapi/v1/txt2img"}, "payload": payload}}

    r = requests.post(f"{RUNPOD_BASE}/{endpoint_id}/run", json=body, headers=headers, timeout=30)
    if not r.ok:
        raise RunPodError(f"RunPod submit failed: {r.status_code} {r.text[:200]}")
    job = r.json()
    job_id = job.get("id")
    if not job_id:
        raise RunPodError(f"RunPod submit returned no job id: {job}")

    status_url = f"{RUNPOD_BASE}/{endpoint_id}/status/{job_id}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = requests.get(status_url, headers=headers, timeout=30)
        if not r.ok:
            raise RunPodError(f"RunPod status check failed: {r.status_code} {r.text[:200]}")
        data = r.json()
        status = data.get("status")
        if status == "COMPLETED":
            output = data.get("output") or {}
            if isinstance(output, dict) and "images" in output:
                return output
            raise RunPodError(f"RunPod job completed with unexpected output shape: {str(output)[:200]}")
        if status in ("FAILED", "CANCELLED", "TIMED_OUT"):
            raise RunPodError(f"RunPod job {status}: {str(data.get('error') or data)[:300]}")
        time.sleep(POLL_INTERVAL)
    raise RunPodError(f"RunPod job timed out after {timeout:.0f}s (job {job_id} still running -- cold start can take ~3 min)")
