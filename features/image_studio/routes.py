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

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from core import config as cfg
from core import forge_dispatch
from core import image_presets
from core.animate_bridge import animate_image
from core.minor_safety import nsfw_render_blocked

log = logging.getLogger(__name__)
router = APIRouter()

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"


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

    subject = body.get("subject")
    if subject is None:
        subject = "auto"
    elif not isinstance(subject, str) or subject not in image_presets.SUBJECT_CHOICES:
        raise HTTPException(400, f"subject must be one of {image_presets.SUBJECT_CHOICES}")

    creature = bool(body.get("creature") is True)

    config = cfg.load()
    width     = _safe_int(body.get("width"),  config.get("chat_image_width", 1024))
    height    = _safe_int(body.get("height"), config.get("chat_image_height", 1024))
    steps     = _safe_int(body.get("steps"),  config.get("chat_image_steps", 25))
    cfg_scale = _safe_float(body.get("cfg"),  config.get("chat_image_cfg", 5.0))
    seed      = _safe_int(body.get("seed"), -1)
    # Clamp to sane SDXL ranges -- a hand-edited or bad value would otherwise
    # go straight into the Forge payload (width=8192 hangs the GPU) or crash
    # Forge outright (an out-of-range seed throws a C-level overflow there,
    # which then leaked as a raw 502 to the client).
    width     = max(256, min(2048, width))
    height    = max(256, min(2048, height))
    steps     = max(1, min(80, steps))
    cfg_scale = max(1.0, min(15.0, cfg_scale))
    # Forge's own random seed generator draws from random.randrange(4294967294)
    # (modules/processing.py) -- an UNSIGNED 32-bit range, not signed int32.
    # The old ceiling (2**31-1) was too low: a seed Forge itself returned from
    # a prior render (e.g. 3324213979) would silently get clamped to a
    # DIFFERENT value here, breaking seed-reproducibility for exactly the
    # comparison testing this tab exists for (caught 2026-07-30 mid-review).
    seed      = max(-1, min(4294967294, seed))

    if preset["nsfw"]:
        # Judge the user's own prompt/negative text (raw, before the preset's
        # style/anatomy wrap is added in build_forge_payload below -- that
        # wrap is static, age-neutral text, so judging pre-wrap vs post-wrap
        # makes no safety difference here, but if the wrap ever grows scene
        # content, judge the wrapped text instead). Already clamped above, so
        # the judge sees the same length ceiling that reaches Forge -- nothing
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

    payload, resolved_subject, creature_applied = image_presets.build_forge_payload(
        preset_key, prompt, negative_prompt, width, height, steps, cfg_scale, seed, subject, creature,
    )
    try:
        result = await asyncio.to_thread(forge_dispatch.txt2img, payload)
    except forge_dispatch.ForgeDispatchError as e:
        return JSONResponse({"error": str(e)}, e.status)

    try:
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

    log.info("[image-studio] generated image (preset=%s, subject=%s, creature=%s/%s, checkpoint=%s) -> %s (seed=%s)",
              preset_key, resolved_subject, creature, creature_applied, preset["checkpoint"], out_path, actual_seed)
    return {
        "image_url":      f"/output/{ts}/{job_slug}/{fname}",
        "image_path":     str(out_path),
        "seed":           actual_seed,
        "checkpoint_used": preset["checkpoint"],
        "adetailer_ran":  ran_adetailer,
        "subject_used":   resolved_subject,
        "creature":       creature,
        "creature_applied": creature_applied,
    }


@router.post("/animate")
async def animate(request: Request):
    body = await _read_json(request)
    return await animate_image(body, label_prefix="Image Studio animate")
