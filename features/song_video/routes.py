"""Song Video API routes -- /api/song-video/*

Upload a song -> analyze it (BPM, key, energy profile) -> generate a music video
that fits the song's duration using chained I2V clips.
"""
import asyncio
import math
import os
import subprocess
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
        await asyncio.to_thread(dest.write_bytes, data)
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
        await asyncio.to_thread(dest.write_bytes, data)
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
        video_prompt    str   story idea / vibe (optional -- AI fills if blank)
        audio_analysis  dict  result from /analyze (optional -- skips re-analysis)
        model           str   WanGP model name
        clip_duration   int   seconds per clip (8-20)
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
        raise HTTPException(400, "Audio file not found -- please re-upload the song")
    if photo_path and not os.path.isfile(photo_path):
        raise HTTPException(400, f"Image not found: {photo_path}")

    config    = cfg.load()
    clip_dur  = max(8, min(15, int(body.get("clip_duration", 10))))
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

    # pad_before / pad_after: seconds of video before song starts / after song ends.
    # Video length = audio_dur + pad_before + pad_after.
    # Audio is delayed pad_before seconds into the final output.
    pad_before = max(0.0, min(10.0, float(body.get("pad_before", 0))))
    pad_after  = max(0.0, min(10.0, float(body.get("pad_after",  0))))
    total_video_dur = audio_dur + pad_before + pad_after

    n_clips = int(body.get("num_clips") or
                  min(12, max(1, math.ceil(total_video_dur / clip_dur))))
    n_clips = max(1, min(12, n_clips))  # cap at 12: longer songs loop, quality stays high

    # Per-job timeout: 5 min per clip + 15 min buffer, min 30 min
    timeout_sec = max(1800, n_clips * 300 + 900)

    settings = {
        "video_prompt":    body.get("video_prompt", ""),
        "variety_theme":   body.get("variety_theme", ""),
        "lyrics_text":     body.get("lyrics_text", ""),
        "user_direction":  body.get("user_direction", "music video, energetic, bold"),
        "audio_path":      audio_path,
        "audio_duration":  total_video_dur,
        "audio_analysis":  analysis,
        "num_clips":       n_clips,
        "clip_duration":   clip_dur,
        "pad_before":      pad_before,
        "pad_after":       pad_after,
        "model_name":      body.get("model", config.get("wan_model", "LTX-2 Dev19B Distilled")),
        "resolution":      body.get("resolution", config.get("resolution", "580p")),
        "override_width":  body.get("output_width"),
        "override_height": body.get("output_height"),
        "video_steps":     body.get("steps",    config.get("fun_video_steps",    30)),
        "video_guidance":  body.get("guidance", config.get("fun_video_guidance", 7.5)),
        "video_seed":      body.get("seed",     config.get("fun_video_seed",     -1)),
        "use_satellite":   bool(body.get("use_satellite", False)),
        "lip_sync":        bool(body.get("lip_sync", True)),
    }
    # Per-model step floors: Distilled caps at 8; everything else floors at 20.
    _mn  = settings["model_name"]
    _raw = int(settings["video_steps"])
    _MODEL_MIN = {
        "LTX-2 Dev19B Distilled": 4,
        "LTX-2 Dev13B":           20,
        "LTX-2 Dev13B 360P":      20,
        "Wan2.1-I2V-14B-480P":    20,
        "Wan2.1-I2V-14B-720P":    20,
    }
    _MODEL_MAX = {"LTX-2 Dev19B Distilled": 8}
    _floor = _MODEL_MIN.get(_mn, 20)
    _ceil  = _MODEL_MAX.get(_mn, 9999)
    settings["video_steps"] = max(_floor, min(_raw, _ceil))
    if settings["video_steps"] != _raw:
        log.info("[song-video] step floor/ceil: ui=%d -> %d for %s", _raw, settings["video_steps"], _mn)

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
        "feature":       "song_video",
        "source_image":  photo_path or "",
        "prompt":        settings.get("video_prompt", "")[:120],
        "model":         settings.get("model_name", ""),
        "num_clips":     n_clips,
        "clip_duration": clip_dur,
        "audio_name":    audio_name,
        "audio_duration": audio_dur,
        "batch_loop":    bool(settings.get("batch_loop", False)),
        # Full settings dict so the queue modal can branch this job back into
        # the source tab and pre-fill all the controls. Strip private fields
        # (those starting with "_") and the audio_analysis blob (large + not
        # a UI control).
        "settings":      {k: v for k, v in settings.items()
                          if not k.startswith("_") and k != "audio_analysis"},
    })
    return {
        "job_id":    job.id,
        "n_clips":   n_clips,
        "clip_dur":  clip_dur,
        "audio_dur": audio_dur,
        "timeout_sec": timeout_sec,
    }


@router.post("/batch/start")
async def batch_start(request: Request):
    """Start a server-side song-video batch.

    Body:
        audio_path    str   path to uploaded audio file (required)
        folder        str   display folder path (informational)
        images        list  [{path: str, name: str}, ...]  absolute image paths
        repeat        bool  loop the folder continuously
        video_prompt  str   optional story direction applied to all images
        model         str   WanGP model
        clip_duration int   seconds per clip
        steps         int
        guidance      float
        output_width  int
        output_height int
    """
    from features.song_video import batch_runner

    body       = await request.json()
    audio_path = body.get("audio_path", "")
    images     = body.get("images", [])
    repeat     = bool(body.get("repeat", False))

    if not audio_path or not os.path.isfile(audio_path):
        raise HTTPException(400, "Audio file not found -- please re-upload the song")
    if not images:
        raise HTTPException(400, "No images provided")

    config   = cfg.load()
    clip_dur = max(8, min(15, int(body.get("clip_duration", 10))))

    # Analyze audio once up-front so every image job skips re-analysis.
    from features.song_video.audio_analyzer import analyze as _analyze
    try:
        analysis = await asyncio.to_thread(_analyze, audio_path, clip_dur)
    except Exception as e:
        log.warning("Batch audio analysis failed, using duration probe: %s", e)
        from core.ffmpeg_utils import probe_duration
        dur      = probe_duration(audio_path)
        analysis = {"duration": dur, "suggested_clip_dur": clip_dur,
                    "suggested_num_clips": max(1, math.ceil(dur / clip_dur))}

    audio_dur = float(analysis.get("duration", 0))
    if audio_dur <= 0:
        from core.ffmpeg_utils import probe_duration
        audio_dur = probe_duration(audio_path)
    if audio_dur <= 0:
        raise HTTPException(400, "Could not determine audio duration")

    pad_before = max(0.0, min(10.0, float(body.get("pad_before", 0))))
    pad_after  = max(0.0, min(10.0, float(body.get("pad_after",  0))))
    total_video_dur = audio_dur + pad_before + pad_after
    n_clips = int(body.get("num_clips") or
                  min(12, max(1, math.ceil(total_video_dur / clip_dur))))
    n_clips = max(1, min(12, n_clips))

    settings = {
        "video_prompt":   body.get("video_prompt", ""),
        "variety_theme":  body.get("variety_theme", ""),
        "lyrics_text":    body.get("lyrics_text", ""),
        "user_direction": body.get("user_direction", "music video, energetic, bold"),
        "audio_path":     audio_path,
        "audio_name":     Path(audio_path).stem[:30],
        "audio_duration": total_video_dur,
        "audio_analysis": analysis,
        "num_clips":      n_clips,
        "clip_duration":  clip_dur,
        "pad_before":     pad_before,
        "pad_after":      pad_after,
        "model_name":     body.get("model", config.get("wan_model", "LTX-2 Dev19B Distilled")),
        "resolution":     body.get("resolution", config.get("resolution", "580p")),
        "override_width": body.get("output_width"),
        "override_height":body.get("output_height"),
        "video_steps":    body.get("steps",    config.get("fun_video_steps",    30)),
        "video_guidance": body.get("guidance", config.get("fun_video_guidance", 7.5)),
        "video_seed":     body.get("seed",     config.get("fun_video_seed",     -1)),
        "use_satellite":  bool(body.get("use_satellite", False)),
    }
    # LTX Distilled sweet spot is 8 steps
    _mn = settings["model_name"]
    if "distilled" in _mn.lower() and "ltx" in _mn.lower():
        settings["video_steps"] = min(int(settings["video_steps"]), 8)

    snapshot = batch_runner.start(
        folder   = body.get("folder", ""),
        images   = [{"path": i["path"], "name": i["name"]} for i in images],
        settings = settings,
        repeat   = repeat,
    )
    return snapshot


@router.post("/batch/resume")
async def batch_resume():
    """Auto-resume a batch that was running when DCS last restarted.

    Called by the Music Video tab on page load. If there is a saved batch
    state the runner restarts from where it left off. Returns the snapshot
    (same shape as /batch/status). If nothing to resume, returns idle state.
    """
    from features.song_video import batch_runner
    saved = batch_runner.load_saved_state()
    if saved:
        log.info("[batch] auto-resuming saved batch (index=%d/%d)",
                 saved.get("index", 0), len(saved.get("images", [])))
        return batch_runner.resume(saved)
    return batch_runner.status()


@router.get("/batch/status")
async def batch_status():
    """Heartbeat + status for the running song-video batch.

    The browser polls this every 5s while the batch is active.
    Each call resets the heartbeat timer; missing it for 120s
    triggers self-termination of the runner.
    """
    from features.song_video import batch_runner
    return batch_runner.status()


@router.post("/batch/stop")
async def batch_stop():
    """Signal the running batch to stop after the current image."""
    from features.song_video import batch_runner
    return batch_runner.stop()


@router.post("/extract-frame")
async def extract_frame(request: Request):
    """Extract the last frame from a video file and save it to uploads/.

    Used by the queue modal's 'Create continuation' button so the next
    generation can start from exactly where the previous video ended.

    Body: { video_path: str }
    Returns: { path, url, width, height }
    """
    from PIL import Image
    from core.ffmpeg_utils import probe_duration

    body = await request.json()
    video_path = body.get("video_path", "")
    if not video_path or not os.path.isfile(video_path):
        raise HTTPException(400, f"Video file not found: {video_path}")

    dur = await asyncio.to_thread(probe_duration, video_path)
    if not dur or dur <= 0:
        raise HTTPException(400, "Could not determine video duration")

    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOADS_DIR / f"{uuid.uuid4().hex[:8]}_lastframe.jpg"
    seek = max(0.0, dur - 0.08)

    r = await asyncio.to_thread(
        subprocess.run,
        ["ffmpeg", "-y", "-ss", f"{seek:.4f}", "-i", video_path,
         "-frames:v", "1", "-q:v", "2", str(dest)],
        capture_output=True, timeout=30,
    )
    if r.returncode != 0 or not dest.exists():
        raise HTTPException(500, "Failed to extract last frame from video")

    with Image.open(str(dest)) as img:
        w, h = img.size
    return {
        "path":   str(dest),
        "url":    f"/uploads/{dest.name}",
        "width":  w,
        "height": h,
    }
