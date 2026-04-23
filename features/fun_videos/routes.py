"""Fun Videos API routes — /api/fun/*

Photo -> AI video + audio pipeline with wildcard support.
"""
import asyncio
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
from core.wangp_models import resolve_model_name
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


_LORA_DIR_MAP = {
    "i2v":            "wan_i2v",
    "i2v_720p":       "wan_i2v",
    "t2v":            "wan",
    "t2v_1.3B":       "wan_1.3B",
    "ltx2_distilled": "ltx2",
    "vace_1.3B":      "wan_5B",
}


@router.get("/loras")
async def list_loras(model: str = ""):
    """Return available LoRA .safetensors files for the given model."""
    wan_root = cfg.get("wan2gp_root") or ""
    if not wan_root:
        return {"loras": [], "directory": ""}
    model_type = resolve_model_name(model) if model else "i2v"
    subdir = _LORA_DIR_MAP.get(model_type, "wan")
    lora_dir = Path(wan_root) / "loras" / subdir
    loras = []
    if lora_dir.exists():
        for f in sorted(lora_dir.glob("*.safetensors")):
            loras.append({"name": f.stem, "path": str(f)})
    return {"loras": loras, "directory": str(lora_dir)}


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
    if provider in ("anthropic", "openai"):
        if not get_key(provider):
            raise HTTPException(503, f"{provider.title()} selected but no API key configured")
        return
    if provider == "ollama":
        # Soft check: if Ollama is reachable, verify the model is installed.
        # If Ollama is temporarily unreachable (GPU pressure, etc.), don't
        # pre-fail — let the actual LLM call handle retries.
        st = ollama_status()
        if st.get("available"):
            needed = cfg.get("ollama_balanced_model") or "qwen3-vl:8b"
            installed = get_ollama_models()
            if installed and not any(m.startswith(needed.split(":")[0]) for m in installed):
                raise HTTPException(
                    503,
                    f"Vision model '{needed}' not found in Ollama. "
                    f"Installed: {', '.join(installed)}. "
                    f"Run: ollama pull {needed}"
                )
        return
    # auto mode: need at least one working provider
    if get_key("anthropic") or get_key("openai"):
        return
    st = ollama_status()
    if not st.get("available"):
        raise HTTPException(503, "No AI provider available — add an Anthropic/OpenAI key or start Ollama")
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

    return await asyncio.to_thread(_analyze, llm_router, b64)


@router.post("/generate-prompts")
async def generate_prompts(request: Request):
    from app import get_llm_router; llm_router = get_llm_router()
    from features.fun_videos.analyzer import generate_video_prompts

    _require_ai()
    body = await request.json()
    image_path = body.get("image_path", "")
    # Resolve URL-style paths (e.g. /output/file.png) the same way /make-it does
    if image_path and not os.path.isfile(image_path):
        clean = image_path.lstrip("/").replace("/", os.sep)
        if clean.startswith(f"output{os.sep}") or clean.startswith("output/"):
            resolved = str(Path(__file__).resolve().parent.parent.parent / clean.replace("/", os.sep))
            if os.path.isfile(resolved):
                image_path = resolved
    if not image_path or not os.path.isfile(image_path):
        raise HTTPException(400, f"Image not found: {image_path}")

    _validate_image(Path(image_path).read_bytes(), Path(image_path).name)
    b64 = encode_image_b64(image_path)
    if not b64:
        raise HTTPException(500, "Failed to encode image")

    config = cfg.load()
    try:
        num_prompts = int(body.get("num_prompts", config.get("fun_num_prompts", 4)))
        return await asyncio.to_thread(
            generate_video_prompts,
            llm_router,
            b64,
            user_direction=body.get("user_direction", ""),
            num_prompts=num_prompts,
            creativity=float(body.get("creativity", config.get("fun_creativity", 8.0))),
            max_tokens=int(body.get("max_tokens", 400 if num_prompts == 1 else 2048)),
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
        # Resolve URL-style paths like /output/file.png → filesystem path
        clean = photo_path.lstrip("/").replace("/", os.sep)
        if clean.startswith(f"output{os.sep}") or clean.startswith("output/"):
            resolved = str(Path(__file__).resolve().parent.parent.parent / clean.replace("/", os.sep))
            if os.path.isfile(resolved):
                photo_path = resolved
    if photo_path and not os.path.isfile(photo_path):
        raise HTTPException(400, f"Photo not found: {photo_path}")
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
        "loras": body.get("loras", []),
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


@router.post("/add-music")
async def add_music(request: Request):
    """Analyze an existing video and add ACE-Step generated music to it.

    Accepts:
        video_path      — path to existing video file
        music_prompt    — optional; if blank the LLM derives it from video frames
        user_direction  — optional free-text guidelines fed to the LLM music director
        instrumental    — bool (default True)
        bpm             — optional int
    """
    from app import get_job_manager, get_llm_router
    job_manager = get_job_manager()

    body = await request.json()
    video_path = body.get("video_path", "")
    if not video_path or not os.path.isfile(video_path):
        raise HTTPException(400, f"Video not found: {video_path}")

    settings = {
        "music_prompt":    body.get("music_prompt", ""),
        "user_direction":  body.get("user_direction", ""),
        "instrumental":    body.get("instrumental", True),
        "bpm":             body.get("bpm"),
        "audio_format":    "mp3",
        "audio_steps":     8,
        "audio_guidance":  7.0,
    }

    def _worker(job, vpath, cfg_settings):
        from app import get_llm_router; llm_router = get_llm_router()
        from features.fun_videos import analyzer, audio_generator
        from features.fun_videos.video_generator import merge_video_audio
        from core.ffmpeg_utils import probe_duration, extract_frame_b64
        import time as _time
        from pathlib import Path as _Path

        job.update(progress=5, message="Analyzing video…")

        music_prompt = cfg_settings.get("music_prompt", "").strip()
        user_direction = cfg_settings.get("user_direction", "").strip()

        if not music_prompt:
            try:
                frames = []
                for pos in [0.1, 0.3, 0.5, 0.7, 0.9]:
                    b64 = extract_frame_b64(vpath, position=pos, max_dim=512)
                    if b64:
                        frames.append(b64)
                if frames:
                    result = analyzer.generate_music_prompt(llm_router, frames, user_direction)
                    music_prompt = result.get("music_prompt", "")
                    if not cfg_settings.get("bpm") and result.get("bpm"):
                        cfg_settings["bpm"] = result["bpm"]
            except Exception as e:
                log.warning("Music analysis failed: %s", e)

        if not music_prompt:
            music_prompt = "cinematic ambient, warm strings, gentle piano"

        job.update(progress=20, message="Generating music…")

        from services.forge_client import unload_checkpoint, reload_checkpoint
        forge_was_unloaded = unload_checkpoint()

        video_dur = probe_duration(vpath)
        audio_dur = min(video_dur + 2.0, 120.0) if video_dur > 0 else 30.0

        def _audio_progress(elapsed_s):
            job.update(progress=20 + min(60, int(elapsed_s / audio_dur * 60)),
                       message=f"Generating music… {elapsed_s:.0f}s elapsed")

        try:
            audio_path, audio_err = audio_generator.generate_audio(
                prompt=music_prompt,
                duration=audio_dur,
                output_dir=str(_Path(vpath).parent),
                audio_format=cfg_settings.get("audio_format", "mp3"),
                bpm=cfg_settings.get("bpm"),
                steps=int(cfg_settings.get("audio_steps", 8)),
                guidance=float(cfg_settings.get("audio_guidance", 7.0)),
                seed=-1,
                lyrics="[inst]",
                instrumental=cfg_settings.get("instrumental", True),
                stop_event=job.stop_event,
                progress_cb=_audio_progress,
            )
        finally:
            if forge_was_unloaded:
                reload_checkpoint()

        if job.stop_event.is_set():
            return
        if not audio_path:
            raise RuntimeError(f"Audio generation failed: {audio_err}")

        job.update(progress=85, message="Mixing audio into video…")

        stem = _Path(vpath).stem
        final = str(_Path(vpath).parent / f"{stem}_with_music_{_time.strftime('%H%M%S')}.mp4")
        merged = merge_video_audio(vpath, audio_path, final)
        if not merged:
            raise RuntimeError("Audio/video merge failed")

        from core.session import get_current as get_session
        try:
            get_session().add_file(_Path(merged).name, "video", "fun_videos", path=merged)
        except Exception:
            pass

        job.output = merged
        job.message = f"Done — music prompt: {music_prompt[:60]}"

    label = f"Add music: {Path(video_path).stem[:24]}"
    job = job_manager.submit(JOB_FUN_VIDEO, _worker, video_path, settings, label=label)
    return {"job_id": job.id}
