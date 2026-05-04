"""Song Video API routes — /api/song-video/*

Upload a song → analyze it (BPM, key, energy profile) → generate a music video
that fits the song's duration using chained I2V clips.
"""
import asyncio
import math
import os
import uuid
import logging
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from core import config as cfg
from core.job_manager import JOB_FUN_MULTI_VIDEO

log = logging.getLogger(__name__)
router = APIRouter()

UPLOADS_DIR  = Path(__file__).resolve().parent.parent.parent / "uploads"
AUDIO_EXTS   = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".mpeg", ".mpg"}
IMAGE_EXTS   = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MAX_AUDIO_MB = 100


@router.post("/upload-audio")
async def upload_audio(files: list[UploadFile] = File(...)):
    """Upload an audio file. Returns path, url, and basic duration probe."""
    saved = []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in AUDIO_EXTS:
            continue
        data = await f.read()
        if len(data) > MAX_AUDIO_MB * 1024 * 1024:
            raise HTTPException(413, f"Audio file exceeds {MAX_AUDIO_MB}MB limit")
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        dest = UPLOADS_DIR / f"{uuid.uuid4().hex[:8]}_{f.filename}"
        dest.write_bytes(data)
        # Basic duration probe (fast, no librosa)
        from core.ffmpeg_utils import probe_duration
        dur = probe_duration(str(dest))
        mins, secs = divmod(int(dur), 60)
        saved.append({
            "path":             str(dest),
            "url":              f"/uploads/{dest.name}",
            "name":             f.filename,
            "duration":         dur,
            "duration_display": f"{mins}:{secs:02d}",
        })
    if not saved:
        raise HTTPException(400, "No valid audio file found (accepted: mp3, wav, flac, ogg, m4a, aac, mpeg)")
    return {"files": saved}


@router.post("/upload-image")
async def upload_image(files: list[UploadFile] = File(...)):
    """Upload an anchor image for visual consistency across clips."""
    from PIL import Image
    import io
    saved = []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in IMAGE_EXTS:
            continue
        data = await f.read()
        try:
            img = Image.open(io.BytesIO(data))
            img.verify()
            img = Image.open(io.BytesIO(data))
        except Exception:
            raise HTTPException(422, f"File '{f.filename}' is not a valid image")
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        dest = UPLOADS_DIR / f"{uuid.uuid4().hex[:8]}_{f.filename}"
        dest.write_bytes(data)
        saved.append({
            "path":   str(dest),
            "url":    f"/uploads/{dest.name}",
            "name":   f.filename,
            "width":  img.size[0],
            "height": img.size[1],
        })
    return {"files": saved}


@router.post("/analyze")
async def analyze_audio(request: Request):
    """Run full audio analysis (BPM, key, energy profile) on an uploaded file.

    Body: { audio_path: str, clip_duration: int | null }
    Returns the full analysis dict from audio_analyzer.analyze().
    """
    body = await request.json()
    audio_path   = body.get("audio_path", "")
    clip_duration = body.get("clip_duration")  # optional override

    if not audio_path or not os.path.isfile(audio_path):
        raise HTTPException(400, f"Audio file not found: {audio_path}")

    from features.song_video.audio_analyzer import analyze
    try:
        result = await asyncio.to_thread(
            analyze, audio_path, int(clip_duration) if clip_duration else None
        )
        return result
    except Exception as e:
        log.exception("Audio analysis failed: %s", e)
        raise HTTPException(500, str(e))


@router.post("/generate")
async def generate(request: Request):
    """Submit a song-video generation job.

    Body:
        audio_path      str   path to uploaded audio file (required)
        photo_path      str   optional anchor image path
        video_prompt    str   story idea / vibe (optional — AI fills if blank)
        audio_analysis  dict  result from /analyze (optional — skips re-analysis)
        model           str   WanGP model name
        clip_duration   int   seconds per clip (8–20)
        num_clips       int   override clip count (auto-calculated from duration if omitted)
        steps           int
        guidance        float
        output_width    int   optional resolution override
        output_height   int
    """
    from app import get_job_manager
    from features.song_video.pipeline import run_song_prep, run_song_pipeline
    job_manager = get_job_manager()

    body       = await request.json()
    audio_path = body.get("audio_path", "")
    photo_path = body.get("photo_path", "") or ""

    if not audio_path or not os.path.isfile(audio_path):
        raise HTTPException(400, "Audio file not found — please re-upload the song")
    if photo_path and not os.path.isfile(photo_path):
        raise HTTPException(400, f"Image not found: {photo_path}")

    config    = cfg.load()
    clip_dur  = max(8, min(20, int(body.get("clip_duration", 8))))
    analysis  = body.get("audio_analysis") or {}

    # If analysis not provided, run a fast probe for duration
    if not analysis:
        from features.song_video.audio_analyzer import analyze as _analyze
        try:
            analysis = await asyncio.to_thread(_analyze, audio_path, clip_dur)
        except Exception as e:
            log.warning("Quick analysis failed: %s", e)
            from core.ffmpeg_utils import probe_duration
            dur = probe_duration(audio_path)
            analysis = {"duration": dur, "suggested_clip_dur": clip_dur,
                        "suggested_num_clips": max(1, math.ceil(dur / clip_dur))}

    audio_dur = float(analysis.get("duration", 0))
    if audio_dur <= 0:
        from core.ffmpeg_utils import probe_duration
        audio_dur = probe_duration(audio_path)
    if audio_dur <= 0:
        raise HTTPException(400, "Could not determine audio duration")

    n_clips = int(body.get("num_clips") or analysis.get("suggested_num_clips") or
                  max(1, math.ceil(audio_dur / clip_dur)))
    n_clips = max(1, min(50, n_clips))  # hard cap at 50 clips

    # Per-job timeout: 5 min per clip + 15 min buffer, min 30 min
    timeout_sec = max(1800, n_clips * 300 + 900)

    settings = {
        "video_prompt":    body.get("video_prompt", ""),
        "variety_theme":   body.get("variety_theme", ""),
        "lyrics_text":     body.get("lyrics_text", ""),
        "user_direction":  body.get("user_direction", "music video, cinematic, energetic"),
        "audio_path":      audio_path,
        "audio_duration":  audio_dur,
        "audio_analysis":  analysis,
        "num_clips":       n_clips,
        "clip_duration":   clip_dur,
        "model_name":      body.get("model", config.get("wan_model", "LTX-2 Dev19B Distilled")),
        "resolution":      body.get("resolution", config.get("resolution", "580p")),
        "override_width":  body.get("output_width"),
        "override_height": body.get("output_height"),
        "video_steps":     body.get("steps",    config.get("fun_video_steps",    30)),
        "video_guidance":  body.get("guidance", config.get("fun_video_guidance", 7.5)),
        "video_seed":      body.get("seed",     config.get("fun_video_seed",     -1)),
    }

    audio_name = Path(audio_path).stem[:20]
    label = f"Music video: {audio_name} ({n_clips} clips)"

    try:
        job = job_manager.submit_with_prep(
            JOB_FUN_MULTI_VIDEO,
            run_song_prep,
            run_song_pipeline,
            photo_path or None,
            settings,
            label=label,
            timeout_seconds=timeout_sec,
        )
    except RuntimeError as e:
        raise HTTPException(429, str(e))

    job.meta.update({
        "source_image":  photo_path or "",
        "prompt":        settings.get("video_prompt", "")[:120],
        "model":         settings.get("model_name", ""),
        "num_clips":     n_clips,
        "clip_duration": clip_dur,
        "audio_name":    audio_name,
        "audio_duration": audio_dur,
    })
    return {
        "job_id":    job.id,
        "n_clips":   n_clips,
        "clip_dur":  clip_dur,
        "audio_dur": audio_dur,
        "timeout_sec": timeout_sec,
    }
