"""Chat Studio routes -- /api/chat-studio/*

A conversational front end for the creative loop: chat with an LLM, ask for an
image, review/adjust the proposed prompt, generate via Forge, refine through
more conversation, and animate the result into a short video. The browser owns
the transcript and prompt-card state; these routes are the LLM/Forge/job-queue
proxy underneath it -- mirrors the pattern in features/manager/routes.py and
the brainstorm endpoint in features/fun_videos/routes.py.
"""
import asyncio
import base64
import json
import logging
import os
import time
import uuid
from pathlib import Path

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from core import config as cfg
from core.job_manager import JOB_FUN_VIDEO

log = logging.getLogger(__name__)
router = APIRouter()

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"
FORGE_TXT2IMG_URL = "http://127.0.0.1:7861/sdapi/v1/txt2img"


async def _read_json(request: Request) -> dict:
    """Parse the request body, turning malformed JSON into a clean 400
    instead of an unhandled 500 (curl/script callers hit this; the app's
    own fetch() never should)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object body required")
    return body


def _in_output_dir(p: str) -> bool:
    """Client-supplied image paths must live under output/ -- keeps a raw API
    call from feeding arbitrary disk files to the vision LLM or copying them
    into the HTTP-served output tree."""
    try:
        return Path(p).resolve().is_relative_to(OUTPUT_DIR.resolve())
    except (OSError, ValueError):
        return False


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


# -- System prompt --------------------------------------------------------------
# Bakes in Andrew's standing SD prompt rules so every image_prompt the model
# proposes is already close to usable -- the user still gets to edit anything
# in the prompt card before Generate is clicked.
SYSTEM_PROMPT = """You are the creative assistant embedded in Drop Cat Go Studio's Chat tab. \
You talk with the user like a helpful collaborator: chat normally, help them shape an idea \
into an AI image, let them review and adjust the image prompt before anything renders, and \
turn a finished image into a short animated video when they ask for that.

You ALWAYS reply with ONE JSON object and nothing else -- no prose outside the JSON, no \
markdown code fences -- shaped exactly like this:
{"reply": "<your conversational reply, 1-3 sentences>", "image_prompt": {"prompt": "...", "negative": "...", "width": 1024, "height": 1024} or null, "video_prompt": "..." or null}

- Set "image_prompt" whenever the user wants a NEW image, or wants CHANGES to the current one. \
Leave it null for plain conversation (greetings, questions, small talk that isn't about an image).
- Set "video_prompt" ONLY when the user explicitly asks to animate, move, or make a video of the \
CURRENT image. Leave it null otherwise -- never propose animation unprompted.
- "reply" is always required -- it is the only thing the user reads as your chat message, so it \
must stand on its own (do not assume they can see the raw JSON).

IMAGE PROMPT RULES -- follow these exactly whenever you set "image_prompt":
1. Order tokens subject-first: medium > subject + attributes > action > clothing > setting > \
framing > lighting > palette > 2-3 style anchors max. Keep attributes adjacent to the noun they \
describe.
2. Stay under 75 tokens for the positive prompt.
3. NEVER use negation in the positive prompt -- "no hat" draws a hat. Describe the presence of \
what you want instead ("clean-shaven" not "no beard", "bare head" not "no hat").
4. The negative prompt starts EMPTY. Only add to it when the user reports an actual defect they \
saw in a render (extra fingers, warped eyes, etc.) -- max 0-15 tokens, never speculative.
5. Be concrete, not evocative -- describe the photograph you want in frame, not the feeling it \
should evoke.
6. Assume an SDXL checkpoint: CFG 4-8, native resolution around 1024x1024 -- adjust width/height \
for a different aspect ratio while staying near that pixel budget.

You are proposing a starting point, not a final answer -- the user can edit any field in the \
prompt card before Generate is clicked."""


# Words that signal the message is plain conversation rather than about the
# current image. Cheap heuristic so a trivial reply ("thanks", "ok") doesn't
# burn a vision call just because an image happens to still be loaded.
_NON_REFINEMENT_STARTERS = (
    "hi", "hello", "hey", "thanks", "thank you", "ok", "okay", "cool", "nice",
    "great", "awesome", "sure", "yes", "no", "what's up", "who are you",
)


def _looks_like_refinement(message: str) -> bool:
    """True if the message plausibly refers to / wants changes to the current
    image, in which case /message sends the image to the model as vision
    context rather than a plain text call."""
    low = (message or "").strip().lower()
    if not low:
        return False
    return not any(low.startswith(w) for w in _NON_REFINEMENT_STARTERS)


@router.post("/message")
async def chat_message(request: Request):
    from app import get_llm_router
    from core.llm_client import TIER_BALANCED, parse_json_response, encode_image_b64

    body = await _read_json(request)
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "message required")
    history = [h for h in (body.get("history") or []) if isinstance(h, dict)]
    current_prompt = (body.get("current_prompt") or "").strip()
    image_path = body.get("image_path") or ""

    ctx_parts = []
    if current_prompt:
        ctx_parts.append(f"Current image prompt: {current_prompt}")
    context_str = "\n".join(ctx_parts) or "No image generated yet."
    user_content = f"{context_str}\n\nUser: {message}"

    hist_lines = []
    for h in history[-20:]:
        role = "Assistant" if h.get("role") == "assistant" else "User"
        hist_lines.append(f"{role}: {(h.get('content') or '').strip()}")
    vision_prompt = ("\n".join(hist_lines) + "\n\n" + user_content) if hist_lines else user_content
    msgs = [
        {"role": h.get("role"), "content": str(h.get("content") or "")}
        for h in history[-20:] if h.get("role") in ("user", "assistant")
    ]
    msgs.append({"role": "user", "content": user_content})

    llm_router = get_llm_router()

    has_image = bool(image_path and os.path.isfile(image_path) and _in_output_dir(image_path))
    use_vision = has_image and _looks_like_refinement(message)

    def _call():
        if use_vision:
            b64 = encode_image_b64(image_path)
            if b64:
                return llm_router.route_vision(
                    vision_prompt, [b64], system=SYSTEM_PROMPT, tier=TIER_BALANCED, max_tokens=900,
                )
        return llm_router.route(msgs, system=SYSTEM_PROMPT, tier=TIER_BALANCED, max_tokens=900)

    try:
        raw = await asyncio.to_thread(_call)
    except Exception as e:
        msg = str(e)
        if "rate limit" in msg.lower() or "429" in msg:
            raise HTTPException(429, "AI rate limit reached -- try again in a moment")
        if "connection" in msg.lower() or "refused" in msg.lower():
            raise HTTPException(503, "AI service unavailable -- check your provider key in Settings")
        raise HTTPException(502, f"AI error -- {msg[:160]}")

    try:
        parsed = parse_json_response(raw or "")
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        data = parsed
    else:
        log.warning("[chat] LLM returned non-JSON response. message=%r raw=%s",
                    message[:120], (raw or "")[:400])
        data = {"reply": (raw or "").strip() or "I didn't get a usable response -- try again."}

    image_prompt = data.get("image_prompt")
    if isinstance(image_prompt, dict) and (image_prompt.get("prompt") or "").strip():
        config = cfg.load()
        image_prompt = {
            "prompt":   str(image_prompt.get("prompt") or "").strip(),
            "negative": str(image_prompt.get("negative") or "").strip(),
            "width":    _safe_int(image_prompt.get("width"), config.get("chat_image_width", 1024)),
            "height":   _safe_int(image_prompt.get("height"), config.get("chat_image_height", 1024)),
        }
    else:
        image_prompt = None

    video_prompt = data.get("video_prompt")
    video_prompt = video_prompt.strip() if isinstance(video_prompt, str) and video_prompt.strip() else None

    try:
        provider_used = llm_router._provider(None)  # noqa: SLF001
    except Exception:
        provider_used = "auto"

    return {
        "reply":         data.get("reply") or (raw or "")[:300] or "...",
        "image_prompt":  image_prompt,
        "video_prompt":  video_prompt,
        "provider_used": provider_used,
    }


@router.post("/generate-image")
async def generate_image(request: Request):
    from core.gpu_orchestrator import gpu

    body = await _read_json(request)
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt required")

    config = cfg.load()
    negative_prompt = body.get("negative_prompt", "")
    width     = _safe_int(body.get("width"),  config.get("chat_image_width", 1024))
    height    = _safe_int(body.get("height"), config.get("chat_image_height", 1024))
    steps     = _safe_int(body.get("steps"),  config.get("chat_image_steps", 25))
    cfg_scale = _safe_float(body.get("cfg"),  config.get("chat_image_cfg", 5.0))
    seed      = _safe_int(body.get("seed"), -1)
    # Clamp to sane SDXL ranges -- LLM-proposed or hand-edited values would
    # otherwise go straight into the Forge payload (width=8192 hangs the GPU).
    width     = max(256, min(2048, width))
    height    = max(256, min(2048, height))
    steps     = max(1, min(80, steps))
    cfg_scale = max(1.0, min(15.0, cfg_scale))

    # Forge and WanGP share the same physical GPU -- Forge is not DCS-managed
    # (see core/gpu_orchestrator.py), but a live WanGP render must not be
    # starved of VRAM by a manual image generation.
    if gpu.is_wangp_rendering():
        return JSONResponse(
            {"error": "Video render in progress -- image generation waits for the GPU"}, 409,
        )

    payload = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "width": width,
        "height": height,
        "steps": steps,
        "cfg_scale": cfg_scale,
        "seed": seed,
        "batch_size": 1,
        "sampler_name": "DPM++ 3M SDE",
    }
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
    try:
        info = json.loads(result.get("info") or "{}")
        if info.get("seed") is not None:
            actual_seed = info.get("seed")
    except Exception:
        pass

    ts = time.strftime("%Y-%m-%d")
    job_slug = f"chat_{uuid.uuid4().hex[:8]}"
    out_dir = OUTPUT_DIR / ts / job_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"img_{time.strftime('%H%M%S')}.png"
    out_path = out_dir / fname
    out_path.write_bytes(img_bytes)

    log.info("[chat] generated image -> %s (seed=%s)", out_path, actual_seed)
    return {
        "image_url":  f"/output/{ts}/{job_slug}/{fname}",
        "image_path": str(out_path),
        "seed":       actual_seed,
    }


@router.post("/animate")
async def animate(request: Request):
    from app import get_job_manager
    from features.fun_videos.pipeline import run_prep, run_pipeline
    from features.fun_videos.video_generator import MODELS as _VG_MODELS

    body = await _read_json(request)
    image_path = body.get("image_path") or ""
    if not image_path or not os.path.isfile(image_path):
        raise HTTPException(400, f"Image not found: {image_path}")
    if not _in_output_dir(image_path):
        raise HTTPException(400, "image_path must be inside the output directory")
    video_prompt = (body.get("video_prompt") or "").strip()
    if not video_prompt:
        raise HTTPException(400, "video_prompt required")

    config = cfg.load()
    requested_model = config.get("wan_model") or "LTX-2 Dev19B Distilled"

    # Reject T2V models upfront -- WanGP would otherwise silently drop the
    # start image and the job fails with an opaque error (same guard as
    # /api/fun/make-it).
    model_def = _VG_MODELS.get(requested_model)
    if model_def is not None and not model_def.get("i2v", True):
        raise HTTPException(
            400,
            f"{requested_model} is text-to-video and cannot accept a start image. "
            f"Pick an I2V model in Settings.",
        )

    duration = _safe_float(body.get("duration"), config.get("fun_video_duration", 6.0))
    duration = max(1.0, min(20.0, duration))

    # Per-model minimum steps -- mirrors _MODEL_MIN_STEPS_SINGLE in
    # features/fun_videos/routes.py make-it (a handler-local dict there, so it
    # cannot be imported; keep the two in sync).
    _min_steps = {
        "LTX-2 Dev19B Distilled": 4,
        "LTX-2 Dev13B":            20,
        "LTX-2 Dev13B 360P":       20,
        "Wan2.1-I2V-14B-480P":     20,
        "Wan2.1-I2V-14B-720P":     20,
        "Wan2.1-T2V-14B":          20,
        "Wan2.1-T2V-1.3B":         15,
    }.get(requested_model, 20)
    video_steps = max(_safe_int(config.get("fun_video_steps"), 30), _min_steps)

    settings = {
        "video_prompt":   video_prompt,
        "skip_audio":     True,
        "model_name":     requested_model,
        "resolution":     config.get("resolution", "580p"),
        "video_duration": duration,
        "video_steps":    video_steps,
        "video_guidance": config.get("fun_video_guidance", 7.5),
        "video_seed":     config.get("fun_video_seed", -1),
        "use_wildcards":  False,
        "instrumental":   True,
        "upscale":        False,
    }

    label = f"Chat animate: {Path(image_path).stem[:20]}"
    job_manager = get_job_manager()
    try:
        job = job_manager.submit_with_prep(
            JOB_FUN_VIDEO, run_prep, run_pipeline, image_path, settings, label=label,
        )
    except RuntimeError as e:
        raise HTTPException(429, str(e))

    job.meta.update({
        "feature":      "fun_video",
        "source_image": image_path,
        "prompt":       settings.get("video_prompt", "")[:120],
        "model":        settings.get("model_name", ""),
        "settings": {
            "prompt":       settings.get("video_prompt", "")[:240],
            "steps":        settings.get("video_steps"),
            "guidance":     settings.get("video_guidance"),
            "duration_sec": settings.get("video_duration"),
            "source_image": image_path,
            "model":        settings.get("model_name", ""),
            "seed":         settings.get("video_seed"),
        },
    })
    return {"job_id": job.id}
