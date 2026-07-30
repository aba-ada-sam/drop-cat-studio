"""Image Studio routes -- /api/image-studio/*

A dedicated, manual image-creation surface (as opposed to Chat's
conversational one): direct prompt/negative/size/steps/cfg controls plus a
preset selector (core/image_presets) that can swap Forge's loaded checkpoint
and layer on Andrew's proven NSFW recipe (checkpoint + LoRA wrap + safety-floor
negatives + ADetailer). Generated images feed into the same Animate ->
fun_videos pipeline as Chat Studio via the shared core/animate_bridge.
"""
import asyncio
import base64
import json
import logging
import time
import uuid
from pathlib import Path

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from core import config as cfg
from core import image_presets
from core.animate_bridge import animate_image
from core.minor_safety import nsfw_render_blocked

log = logging.getLogger(__name__)
router = APIRouter()

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"
FORGE_TXT2IMG_URL = "http://127.0.0.1:7861/sdapi/v1/txt2img"


async def _read_json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object body required")
    return body


def _safe_int(val, default: int) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _safe_float(val, default: float) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


@router.get("/presets")
async def presets():
    return {"presets": image_presets.list_presets()}


@router.post("/generate")
async def generate(request: Request):
    from core.gpu_orchestrator import gpu

    body = await _read_json(request)
    raw_prompt = body.get("prompt")
    prompt = raw_prompt.strip() if isinstance(raw_prompt, str) else ""
    if not prompt:
        raise HTTPException(400, "prompt required")
    prompt = image_presets.clamp_prompt_text(prompt)

    raw_negative = body.get("negative_prompt")
    negative_prompt = image_presets.clamp_prompt_text(raw_negative if isinstance(raw_negative, str) else "")

    preset_key = body.get("preset")
    if not isinstance(preset_key, str) or preset_key not in image_presets.PRESETS:
        preset_key = image_presets.DEFAULT_PRESET_KEY
    preset = image_presets.get_preset(preset_key)

    config = cfg.load()
    width     = _safe_int(body.get("width"),  config.get("chat_image_width", 1024))
    height    = _safe_int(body.get("height"), config.get("chat_image_height", 1024))
    steps     = _safe_int(body.get("steps"),  config.get("chat_image_steps", 25))
    cfg_scale = _safe_float(body.get("cfg"),  config.get("chat_image_cfg", 5.0))
    seed      = _safe_int(body.get("seed"), -1)
    # Clamp to sane SDXL ranges -- a hand-edited or bad value would otherwise
    # go straight into the Forge payload (width=8192 hangs the GPU) or crash
    # Forge outright (an out-of-int32-range seed throws a C-level overflow
    # there, which then leaked as a raw 502 to the client).
    width     = max(256, min(2048, width))
    height    = max(256, min(2048, height))
    steps     = max(1, min(80, steps))
    cfg_scale = max(1.0, min(15.0, cfg_scale))
    seed      = max(-1, min(2**31 - 1, seed))

    if preset["nsfw"]:
        # Judge the FINAL wrapped scene text (user prompt is the subject; the
        # preset wrap itself carries no age-relevant content, but judging the
        # combined text is the conservative choice -- never judge less than
        # what's about to render). prompt/negative_prompt are already clamped
        # above, so the judge sees exactly what will reach Forge -- nothing
        # padded past the judge's own text window can hide from it.
        scene_text = f"{prompt}\n{negative_prompt}".strip()
        blocked = await nsfw_render_blocked(scene_text)
        if blocked:
            return JSONResponse(
                {"error": "Blocked by safety check -- this request could not be verified as depicting adults only."},
                403,
            )

    # Forge and WanGP share the same physical GPU -- Forge is not DCS-managed
    # (see core/gpu_orchestrator.py), but a live WanGP render must not be
    # starved of VRAM by a manual image generation.
    if gpu.is_wangp_rendering():
        return JSONResponse(
            {"error": "Video render in progress -- image generation waits for the GPU"}, 409,
        )

    payload = image_presets.build_forge_payload(
        preset_key, prompt, negative_prompt, width, height, steps, cfg_scale, seed,
    )
    try:
        r = await asyncio.to_thread(requests.post, FORGE_TXT2IMG_URL, json=payload, timeout=600)
    except requests.exceptions.ConnectionError:
        return JSONResponse(
            {"error": "Forge is not running on :7861 -- start it from its own GUI"}, 503,
        )
    except requests.exceptions.RequestException as e:
        return JSONResponse({"error": f"Forge request failed: {e}"}, 502)

    if not r.ok:
        return JSONResponse({"error": f"Forge returned {r.status_code}: {r.text[:200]}"}, 502)

    try:
        result = r.json()
        images = result.get("images") or []
        img_bytes = base64.b64decode(images[0]) if images else None
    except Exception as e:
        return JSONResponse({"error": f"Forge response unreadable: {e}"}, 502)
    if not img_bytes:
        return JSONResponse({"error": "Forge returned no image"}, 502)

    actual_seed = seed
    ran_adetailer = False
    try:
        info = json.loads(result.get("info") or "{}")
        if info.get("seed") is not None:
            actual_seed = info.get("seed")
        ran_adetailer = "ADetailer model" in (info.get("extra_generation_params") or {})
    except Exception:
        pass

    ts = time.strftime("%Y-%m-%d")
    job_slug = f"image_{uuid.uuid4().hex[:8]}"
    out_dir = OUTPUT_DIR / ts / job_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"img_{time.strftime('%H%M%S')}.png"
    out_path = out_dir / fname
    out_path.write_bytes(img_bytes)

    log.info("[image-studio] generated image (preset=%s, checkpoint=%s) -> %s (seed=%s)",
              preset_key, preset["checkpoint"], out_path, actual_seed)
    return {
        "image_url":      f"/output/{ts}/{job_slug}/{fname}",
        "image_path":     str(out_path),
        "seed":           actual_seed,
        "checkpoint_used": preset["checkpoint"],
        "adetailer_ran":  ran_adetailer,
    }


@router.post("/animate")
async def animate(request: Request):
    body = await _read_json(request)
    return await animate_image(body, label_prefix="Image Studio animate")
