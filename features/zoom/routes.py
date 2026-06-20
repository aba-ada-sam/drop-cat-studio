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

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def extract_frame_from_video(video_path: str, out_png: str, position: str = "last") -> bool:
    """Extract a frame from a video file to PNG. position: "first" (0.1s) or
    "last" (0.5s before EOF). Returns True on success. (Relocated here from the
    retired features/zoom/pipeline.py.)"""
    try:
        if position == "last":
            cmd = ["ffmpeg", "-y", "-sseof", "-0.5", "-i", video_path,
                   "-frames:v", "1", "-q:v", "1", out_png]
        else:
            cmd = ["ffmpeg", "-y", "-ss", "0.1", "-i", video_path,
                   "-frames:v", "1", "-q:v", "1", out_png]
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        return r.returncode == 0 and os.path.isfile(out_png)
    except Exception as e:
        log.warning("[zoom] Frame extraction failed: %s", e)
        return False


@router.post("/api/zoom/make")
async def zoom_make(request: Request):
    """Submit an outpaint/inpaint zoom-IN job (Forge SD detail dive).

    Zoom-OUT was removed -- this feature is zoom-IN only. The source image is the
    starting view; AI paints ever-finer detail in the centre as the camera dives,
    and the levels are dissolved together so there are no visible clip joins.

    Body fields:
      source_path  -- absolute path to a photo OR video file
      idea         -- optional description of the detail to dive into
      n_levels     -- detail levels / zoom depth (default 7)
      skip_audio   -- bool (default false)
      instrumental -- bool (default false)
      music_prompt -- optional music override
    """
    from app import get_job_manager
    from core.job_manager import JOB_FUN_MULTI_VIDEO
    from features.zoom.outpaint_zoom import run_oz_prep, run_oz_pipeline

    job_manager = get_job_manager()

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    source_path = body.get("source_path", "").strip()
    if not source_path or not os.path.isfile(source_path):
        return JSONResponse({"error": "source_path is required and must exist"}, status_code=400)

    # Video source -> dive into its first frame.
    _tmp_dir_to_clean: str | None = None
    ext = Path(source_path).suffix.lower()
    if ext in (".mp4", ".mov", ".avi", ".webm", ".mkv"):
        _tmp_dir_to_clean = tempfile.mkdtemp(prefix="dcs_zoom_")
        frame_png = os.path.join(_tmp_dir_to_clean, "start_frame.png")
        if not extract_frame_from_video(source_path, frame_png, position="first"):
            return JSONResponse({"error": "Could not extract frame from video"}, status_code=422)
        source_path = frame_png

    settings = {
        "zoom_direction": "in",
        "idea":         body.get("idea", "").strip(),
        "n_levels":     max(3, min(12, int(body.get("n_levels", 7)))),
        "zoom_factor":  float(body.get("zoom_factor", 0.72)),
        "sec_per_level": float(body.get("sec_per_level", 2.2)),
        "denoise":      float(body.get("denoise", 0.40)),
        "skip_audio":   bool(body.get("skip_audio", False)),
        "instrumental": bool(body.get("instrumental", False)),
        "music_prompt": body.get("music_prompt", ""),
        "audio_format": body.get("audio_format", "mp3"),
        "_tmp_dir":     _tmp_dir_to_clean,
    }

    label = f"Zoom in: {Path(source_path).stem[:20]}"
    try:
        job = job_manager.submit_with_prep(
            JOB_FUN_MULTI_VIDEO, run_oz_prep, run_oz_pipeline,
            source_path, settings, label=label, timeout_seconds=600,
        )
    except RuntimeError as e:
        raise __import__("fastapi").HTTPException(429, str(e))

    job.meta["feature"] = "zoom"
    job.meta["zoom_direction"] = "in"
    return {"job_id": job.id, "label": label}


# /api/zoom/extend removed -- zoom-out is gone and the old WanGP
# chain it used is retired. Continue-zoom isn't part of the outpaint zoom-in.



_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".webm", ".mkv"}
_ALL_EXTS   = _IMAGE_EXTS | _VIDEO_EXTS


@router.post("/api/zoom/scan-folder")
async def zoom_scan_folder(request: Request):
    """Return sorted list of images and videos in a folder."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    folder = body.get("folder", "").strip()
    if not folder or not os.path.isdir(folder):
        return JSONResponse({"error": "folder must be an existing directory"}, status_code=400)

    files = sorted(
        [
            {"path": str(p), "name": p.name, "is_video": p.suffix.lower() in _VIDEO_EXTS}
            for p in Path(folder).iterdir()
            if p.suffix.lower() in _ALL_EXTS and p.is_file()
        ],
        key=lambda x: x["name"].lower(),
    )
    return {"folder": folder, "files": files, "total": len(files)}


@router.post("/api/zoom/folder-loop/start")
async def zoom_folder_loop_start(request: Request):
    """Start a server-side zoom folder loop."""
    import asyncio as _asyncio
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    folder = body.get("folder", "").strip()
    if not folder or not os.path.isdir(folder):
        return JSONResponse({"error": "folder must be an existing directory"}, status_code=400)

    from features.zoom.folder_loop import ALL_EXTS, start as _loop_start

    files = sorted(
        [
            {"path": str(p), "name": p.name, "is_video": p.suffix.lower() in _VIDEO_EXTS}
            for p in Path(folder).iterdir()
            if p.suffix.lower() in ALL_EXTS and p.is_file()
        ],
        key=lambda x: x["name"].lower(),
    )
    if not files:
        return JSONResponse({"error": "No supported image or video files found in that folder"}, status_code=400)

    settings = {
        "zoom_direction":  "in",
        "idea":            body.get("idea", "").strip(),
        "n_levels":        max(3, min(12, int(body.get("n_levels", 7)))),
        "zoom_factor":     float(body.get("zoom_factor", 0.72)),
        "sec_per_level":   float(body.get("sec_per_level", 2.2)),
        "denoise":         float(body.get("denoise", 0.40)),
        "skip_audio":      bool(body.get("skip_audio", False)),
        "instrumental":    bool(body.get("instrumental", False)),
        "music_prompt":    body.get("music_prompt", ""),
        "audio_format":    body.get("audio_format", "mp3"),
        "_timeout_seconds": 600,
    }

    repeat = bool(body.get("repeat", False))
    snap = await _asyncio.to_thread(_loop_start, folder, files, settings, repeat)
    return snap


@router.get("/api/zoom/folder-loop/status")
async def zoom_folder_loop_status():
    """Heartbeat + state for the folder loop."""
    from features.zoom.folder_loop import status as _loop_status
    return _loop_status()


@router.post("/api/zoom/folder-loop/stop")
async def zoom_folder_loop_stop():
    """Stop the folder loop."""
    from features.zoom.folder_loop import stop as _loop_stop
    _loop_stop()
    return {"ok": True}


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
    shutil.rmtree(tmp_dir, ignore_errors=True)

    return {"frame_path": str(dest), "frame_url": f"/uploads/{dest.name}"}
