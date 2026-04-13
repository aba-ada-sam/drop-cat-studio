"""Fun Videos API routes — /api/fun/*

Photo -> AI video + audio pipeline with wildcard support.
"""
import io
import logging
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from PIL import Image

from core import config as cfg
from core.job_manager import JOB_FUN_VIDEO
from core.llm_client import encode_image_b64
from features.fun_videos.video_generator import MODELS

log = logging.getLogger(__name__)
router = APIRouter()

UPLOADS_DIR = Path(__file__).resolve().parent.parent.parent / "uploads"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
MAX_IMAGE_MB = 15


def _validate_image(data: bytes, filename: str) -> Image.Image:
    """Validate image data. Returns the PIL Image on success, raises HTTPException on failure."""
    if len(data) > MAX_IMAGE_MB * 1024 * 1024:
        raise HTTPException(422, f"Image '{filename}' exceeds {MAX_IMAGE_MB}MB limit.")
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        # Re-open after verify (verify leaves the file in an unusable state)
        img = Image.open(io.BytesIO(data))
    except Exception:
        raise HTTPException(422, f"File '{filename}' is not a valid image.")
    return img


@router.post("/upload")
async def upload_photo(files: list[UploadFile] = File(...)):
    saved = []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in IMAGE_EXTS:
            continue
        data = await f.read()
        img = _validate_image(data, f.filename or "unknown")
        dest = UPLOADS_DIR / f"{uuid.uuid4().hex[:8]}_{f.filename}"
        dest.write_bytes(data)
        saved.append({
            "path": str(dest),
            "name": f.filename,
            "width": img.size[0],
            "height": img.size[1],
            "url": f"/uploads/{dest.name}",
        })
    return {"files": saved}


def _require_ai():
    """Raise HTTP 503 if no AI provider is available."""
    from core import config as cfg
    from core.keys import get_key, status as ollama_status, get_ollama_models
    provider = cfg.get("llm_provider") or "auto"
    if provider == "anthropic" or (provider == "auto" and get_key("anthropic")):
        return
    if provider == "openai" or (provider == "auto" and get_key("openai")):
        return
    st = ollama_status()
    if not st.get("available"):
        raise HTTPException(503, "No AI provider available — add an Anthropic/OpenAI key or start Ollama")
    # Check that the vision model is actually installed (not just Ollama running)
    needed = cfg.get("ollama_balanced_model") or "qwen3-vl:8b"
    installed = get_ollama_models()
    if installed and not any(m.startswith(needed.split(":")[0]) for m in installed):
        raise HTTPException(
            503,
            f"Vision model '{needed}' not found in Ollama. "
            f"Installed: {', '.join(installed)}. "
            f"Run: ollama pull {needed}"
        )


@router.post("/analyze-photo")
async def analyze_photo(request: Request):
    from app import get_llm_router; llm_router = get_llm_router()
    from features.fun_videos.analyzer import analyze_photo as _analyze

    _require_ai()
    body = await request.json()
    image_path = body.get("image_path", "")
    if not image_path or not os.path.isfile(image_path):
        raise HTTPException(400, "Image not found")

    _validate_image(Path(image_path).read_bytes(), Path(image_path).name)
    b64 = encode_image_b64(image_path)
    if not b64:
        raise HTTPException(500, "Failed to encode image")

    return _analyze(llm_router, b64)


@router.post("/generate-prompts")
async def generate_prompts(request: Request):
    from app import get_llm_router; llm_router = get_llm_router()
    from features.fun_videos.analyzer import generate_video_prompts

    _require_ai()
    body = await request.json()
    image_path = body.get("image_path", "")
    if not image_path or not os.path.isfile(image_path):
        raise HTTPException(400, "Image not found")

    _validate_image(Path(image_path).read_bytes(), Path(image_path).name)
    b64 = encode_image_b64(image_path)
    if not b64:
        raise HTTPException(500, "Failed to encode image")

    config = cfg.load()
    try:
        return generate_video_prompts(
            llm_router,
            b64,
            user_direction=body.get("user_direction", ""),
            num_prompts=int(body.get("num_prompts", config.get("fun_num_prompts", 4))),
            creativity=float(body.get("creativity", config.get("fun_creativity", 8.0))),
        )
    except RuntimeError as e:
        msg = str(e)
        # Detect AI content-policy refusals and return a clean 422 instead of 500
        if "unparseable" in msg.lower() or "not able to" in msg.lower() or "won't produce" in msg.lower():
            raise HTTPException(422, f"AI declined to generate prompts for this image. Try a different photo or creative direction.")
        raise HTTPException(500, msg)


@router.post("/make-it")
async def make_it(request: Request):
    from app import get_job_manager; job_manager = get_job_manager()
    from features.fun_videos.pipeline import run_pipeline

    body = await request.json()
    photo_path = body.get("photo_path") or ""
    # photo_path is optional for text-to-video (T2V) models
    if photo_path and not os.path.isfile(photo_path):
        raise HTTPException(400, "Photo not found")
    if not photo_path and not body.get("video_prompt", "").strip():
        raise HTTPException(400, "Provide either a photo or a video prompt")

    config = cfg.load()
    settings = {
        "video_prompt": body.get("video_prompt", ""),
        "music_prompt": body.get("music_prompt", ""),
        "lyrics": body.get("lyrics", ""),
        "user_direction": body.get("user_direction", ""),
        "use_wildcards": body.get("use_wildcards", False),
        "video_duration": body.get("duration", config.get("fun_video_duration", 14.0)),
        "model_name": body.get("model", config.get("wan_model", "LTX-2 Dev19B Distilled")),
        "resolution": body.get("resolution", config.get("resolution", "580p")),
        "video_steps": body.get("steps", config.get("fun_video_steps", 30)),
        "video_guidance": body.get("guidance", config.get("fun_video_guidance", 7.5)),
        "video_seed": body.get("seed", config.get("fun_video_seed", -1)),
        "audio_steps": body.get("audio_steps", config.get("fun_audio_steps", 8)),
        "audio_guidance": body.get("audio_guidance", config.get("fun_audio_guidance", 7.0)),
        "instrumental": body.get("instrumental", config.get("fun_audio_instrumental", True)),
        "audio_format": body.get("audio_format", config.get("fun_audio_format", "mp3")),
        "bpm": body.get("bpm"),
        "skip_audio": body.get("skip_audio", False),
        "end_photo_path": body.get("end_photo_path"),
    }

    if photo_path:
        label = f"Fun Video: {Path(photo_path).stem[:20]}"
    else:
        label = f"T2V: {settings.get('video_prompt', '')[:24]}"
    job = job_manager.submit(
        JOB_FUN_VIDEO, run_pipeline, photo_path, settings, label=label,
    )
    return {"job_id": job.id}


@router.get("/models")
async def list_models():
    return {"models": {name: info for name, info in MODELS.items()}}
