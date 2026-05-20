"""Zoom feature routes: /api/zoom/*"""
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()
log = logging.getLogger("zoom")

# Timeout per clip (seconds) -- Wan I2V 14B at 25 steps can take ~10 min each
_PER_CLIP_TIMEOUT_S = 900
_AUDIO_BUFFER_S = 300


@router.post("/api/zoom/make")
async def zoom_make(request: Request):
    """Submit a zoom-in or zoom-out generation job.

    Body fields:
      source_path  -- absolute path to photo OR video file
      zoom_direction -- "in" or "out" (default "out")
      n_clips      -- 3-8 (default 5)
      clip_duration -- seconds per clip before trim (default 5.0)
      model_name   -- WanGP model (default Wan2.1-I2V-14B-480P)
      steps        -- denoising steps (default 25)
      idea         -- optional user description of the zoom content
      skip_audio   -- bool (default false)
      instrumental -- bool (default false)
      music_prompt -- override music prompt
    """
    from app import get_job_manager
    from core.job_manager import JOB_FUN_MULTI_VIDEO
    from features.zoom.pipeline import run_zoom_prep, run_zoom_pipeline, extract_frame_from_video
    from core.wangp_models import resolve_model_name

    job_manager = get_job_manager()

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    source_path = body.get("source_path", "").strip()
    if not source_path or not os.path.isfile(source_path):
        return JSONResponse({"error": "source_path is required and must exist"}, status_code=400)

    direction = body.get("zoom_direction", "out")
    if direction not in ("in", "out"):
        return JSONResponse({"error": "zoom_direction must be 'in' or 'out'"}, status_code=400)

    n_clips = max(2, min(8, int(body.get("n_clips", 5))))
    clip_dur = max(3.0, min(10.0, float(body.get("clip_duration", 5.0))))
    model_name = resolve_model_name(body.get("model_name", "Wan2.1-I2V-14B-480P"))

    # If source is a video, extract the appropriate frame first
    ext = Path(source_path).suffix.lower()
    if ext in (".mp4", ".mov", ".avi", ".webm", ".mkv"):
        frame_pos = "last" if direction == "out" else "first"
        tmp_dir = tempfile.mkdtemp(prefix="dcs_zoom_")
        frame_png = os.path.join(tmp_dir, "start_frame.png")
        ok = extract_frame_from_video(source_path, frame_png, position=frame_pos)
        if not ok:
            return JSONResponse({"error": "Could not extract frame from video"}, status_code=422)
        source_path = frame_png
        log.info("[zoom] Extracted %s frame from video -> %s", frame_pos, frame_png)

    settings = {
        "zoom_direction": direction,
        "n_clips": n_clips,
        "clip_duration": clip_dur,
        "model_name": model_name,
        "steps": int(body.get("steps", 25)),
        "guidance": float(body.get("guidance", 5.0)),
        "idea": body.get("idea", "").strip(),
        "skip_audio": bool(body.get("skip_audio", False)),
        "instrumental": bool(body.get("instrumental", False)),
        "music_prompt": body.get("music_prompt", ""),
        "audio_format": body.get("audio_format", "mp3"),
        "upscale": bool(body.get("upscale", False)),
        "upscale_scale": float(body.get("upscale_scale", 2.0)),
    }

    label = f"Zoom {direction}: {Path(source_path).stem[:20]}"
    timeout = n_clips * _PER_CLIP_TIMEOUT_S + _AUDIO_BUFFER_S

    try:
        job = job_manager.submit_with_prep(
            JOB_FUN_MULTI_VIDEO,
            run_zoom_prep, run_zoom_pipeline,
            source_path, settings,
            label=label,
            timeout_seconds=timeout,
        )
    except RuntimeError as e:
        raise __import__("fastapi").HTTPException(429, str(e))

    return {"job_id": job.id, "label": label}


@router.post("/api/zoom/extract-frame")
async def zoom_extract_frame(request: Request):
    """Extract a specific frame from a video for preview.

    Body: { video_path, time_sec (default: -1 = last frame) }
    Returns: { frame_url } pointing to a served temp file.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    video_path = body.get("video_path", "").strip()
    if not video_path or not os.path.isfile(video_path):
        return JSONResponse({"error": "video_path must exist"}, status_code=400)

    time_sec = float(body.get("time_sec", -1))

    tmp_dir = tempfile.mkdtemp(prefix="dcs_zoomframe_")
    frame_png = os.path.join(tmp_dir, "frame.png")

    from features.zoom.pipeline import extract_frame_from_video
    if time_sec < 0:
        ok = extract_frame_from_video(video_path, frame_png, position="last")
    else:
        try:
            cmd = [
                "ffmpeg", "-y", "-ss", str(time_sec), "-i", video_path,
                "-frames:v", "1", "-q:v", "1", frame_png,
            ]
            r = subprocess.run(cmd, capture_output=True, timeout=20)
            ok = r.returncode == 0 and os.path.isfile(frame_png)
        except Exception:
            ok = False

    if not ok:
        return JSONResponse({"error": "Frame extraction failed"}, status_code=422)

    # Serve via the existing /output thumbnail or uploads path
    # Copy to uploads dir so it's accessible via the file server
    uploads_dir = Path(__file__).resolve().parents[2] / "uploads"
    uploads_dir.mkdir(exist_ok=True)
    dest = uploads_dir / f"zoomframe_{Path(tmp_dir).name}.png"
    import shutil
    shutil.copy2(frame_png, dest)

    return {"frame_path": str(dest), "frame_url": f"/uploads/{dest.name}"}
