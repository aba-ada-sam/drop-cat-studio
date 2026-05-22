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

from features.fun_videos.video_generator import MODELS as _VG_MODELS

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
    from features.fun_videos.video_generator import negative_prompt_for as _neg_for

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

    n_clips = max(2, min(15, int(body.get("n_clips", 5))))
    clip_dur = max(3.0, min(15.0, float(body.get("clip_duration", 4.0))))
    model_name = body.get("model_name", "LTX-2 Dev19B Distilled")
    if model_name not in _VG_MODELS:
        model_name = "LTX-2 Dev19B Distilled"

    # Use the model's native resolution. The 832x480 shortcut was removed --
    # lower resolution compounds artifacts through the chain anchor and produces
    # visibly degraded output by clip 3-4. TeaCache + shorter clip duration
    # already provide the speed improvement without this quality tradeoff.
    _model_res = _VG_MODELS.get(model_name, {}).get("res", (1032, 580))
    _zoom_res = body.get("zoom_res") or _model_res

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

    _model_info = _VG_MODELS.get(model_name, {})
    _default_guidance = _model_info.get("guidance", 3.5)
    _default_steps    = _model_info.get("steps", 25)

    settings = {
        "zoom_direction": direction,
        "n_clips": n_clips,
        "clip_duration": clip_dur,
        "model_name": model_name,
        "zoom_res": list(_zoom_res),
        "steps": int(body.get("steps", _default_steps)),
        "guidance": float(body.get("guidance", _default_guidance)),
        "idea": body.get("idea", "").strip(),
        "skip_audio": bool(body.get("skip_audio", False)),
        "instrumental": bool(body.get("instrumental", False)),
        "music_prompt": body.get("music_prompt", ""),
        "audio_format": body.get("audio_format", "mp3"),
        "audio_first": bool(body.get("audio_first", False)),
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

    job.meta["feature"] = "zoom"
    job.meta["zoom_direction"] = direction
    return {"job_id": job.id, "label": label}


@router.post("/api/zoom/extend")
async def zoom_extend(request: Request):
    """Extend an existing zoom video with N more clips in the same direction.

    Body fields:
      existing_video_path  -- absolute path to the zoom video to extend
      zoom_direction       -- "in" or "out" (must match original; default "out")
      n_clips              -- how many new clips to add (default 3)
      clip_duration        -- seconds per new clip (default 4.0)
      model_name           -- model to use (default LTX-2 Dev19B Distilled)
      idea                 -- optional creative direction for the extension
      music_prompt         -- override music prompt (empty = re-generate)
      skip_audio           -- if true, no audio on the extended video
      instrumental         -- bool
    """
    from app import get_job_manager
    from core.job_manager import JOB_FUN_MULTI_VIDEO
    from features.zoom.pipeline import run_zoom_prep, run_zoom_pipeline, extract_frame_from_video
    import tempfile as _tmp

    job_manager = get_job_manager()

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    existing_path = body.get("existing_video_path", "").strip()
    if not existing_path or not os.path.isfile(existing_path):
        return JSONResponse({"error": "existing_video_path is required and must exist"}, status_code=400)

    direction = body.get("zoom_direction", "out")
    if direction not in ("in", "out"):
        return JSONResponse({"error": "zoom_direction must be 'in' or 'out'"}, status_code=400)

    # Extract the continuation frame: last frame for zoom-out, first frame for zoom-in
    frame_pos = "last" if direction == "out" else "first"
    tmp_dir = _tmp.mkdtemp(prefix="dcs_zoomext_")
    frame_png = os.path.join(tmp_dir, "extend_frame.png")
    ok = extract_frame_from_video(existing_path, frame_png, position=frame_pos)
    if not ok:
        return JSONResponse({"error": "Could not extract frame from existing video"}, status_code=422)

    n_clips = max(2, min(15, int(body.get("n_clips", 3))))
    clip_dur = max(3.0, min(15.0, float(body.get("clip_duration", 4.0))))
    model_name = body.get("model_name", "LTX-2 Dev19B Distilled")
    if model_name not in _VG_MODELS:
        model_name = "LTX-2 Dev19B Distilled"

    _model_res = _VG_MODELS.get(model_name, {}).get("res", (1032, 580))
    _zoom_res = body.get("zoom_res") or _model_res

    _model_info = _VG_MODELS.get(model_name, {})
    settings = {
        "zoom_direction":     direction,
        "n_clips":            n_clips,
        "clip_duration":      clip_dur,
        "model_name":         model_name,
        "zoom_res":           list(_zoom_res),
        "steps":              int(body.get("steps", _model_info.get("steps", 8))),
        "guidance":           float(body.get("guidance", _model_info.get("guidance", 3.0))),
        "idea":               body.get("idea", "").strip(),
        "skip_audio":         bool(body.get("skip_audio", False)),
        "instrumental":       bool(body.get("instrumental", False)),
        "music_prompt":       body.get("music_prompt", ""),
        "audio_format":       body.get("audio_format", "mp3"),
        "audio_steps":        int(body.get("audio_steps", 8)),
        "audio_guidance":     float(body.get("audio_guidance", 7.0)),
        "bpm":                body.get("bpm"),
        "audio_first":        bool(body.get("audio_first", False)),
        "extend_base_path":   existing_path,
        "upscale":            bool(body.get("upscale", False)),
        "upscale_scale":      float(body.get("upscale_scale", 2.0)),
    }

    label = f"Extend zoom {direction}: {Path(existing_path).stem[:20]}"
    timeout = n_clips * _PER_CLIP_TIMEOUT_S + _AUDIO_BUFFER_S

    try:
        job = job_manager.submit_with_prep(
            JOB_FUN_MULTI_VIDEO,
            run_zoom_prep, run_zoom_pipeline,
            frame_png, settings,
            label=label,
            timeout_seconds=timeout,
        )
    except RuntimeError as e:
        raise __import__("fastapi").HTTPException(429, str(e))

    job.meta["feature"] = "zoom"
    job.meta["zoom_direction"] = direction
    log.info("[zoom] Extend job %s: %d clips from %s", job.id, n_clips, Path(existing_path).name)
    return {"job_id": job.id, "label": label}


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

    n_clips  = max(2, min(15, int(body.get("n_clips", 5))))
    clip_dur = max(3.0, min(15.0, float(body.get("clip_duration", 4.0))))
    model_name = body.get("model_name", "LTX-2 Dev19B Distilled")
    if model_name not in _VG_MODELS:
        model_name = "LTX-2 Dev19B Distilled"
    _model_info = _VG_MODELS.get(model_name, {})
    _zoom_res = _model_info.get("res", (1032, 580))

    settings = {
        "zoom_direction":  body.get("zoom_direction", "out"),
        "n_clips":         n_clips,
        "clip_duration":   clip_dur,
        "model_name":      model_name,
        "zoom_res":        list(_zoom_res),
        "steps":           int(body.get("steps", _model_info.get("steps", 8))),
        "guidance":        float(body.get("guidance", _model_info.get("guidance", 3.0))),
        "idea":            body.get("idea", "").strip(),
        "skip_audio":      bool(body.get("skip_audio", False)),
        "instrumental":    bool(body.get("instrumental", False)),
        "music_prompt":    body.get("music_prompt", ""),
        "audio_format":    body.get("audio_format", "mp3"),
        "audio_steps":     int(body.get("audio_steps", 8)),
        "audio_guidance":  float(body.get("audio_guidance", 7.0)),
        "bpm":             body.get("bpm"),
        "audio_first":     bool(body.get("audio_first", False)),
        "_timeout_seconds": n_clips * _PER_CLIP_TIMEOUT_S + _AUDIO_BUFFER_S,
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
