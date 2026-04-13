"""Video Tools API routes — /api/tools/*

Batch video transforms: reverse, mirror, flip, speed, upscale, sharpen.
Also includes the music mixer from Github Video Editor.
"""
import logging
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from core import config as cfg
from core.job_manager import JOB_VIDEO_TOOL
from features.video_tools.reverser import VIDEO_EXTS, process_batch

log = logging.getLogger(__name__)
router = APIRouter()

UPLOADS_DIR = Path(__file__).resolve().parent.parent.parent / "uploads"
OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"


@router.post("/add-paths")
async def add_paths(request: Request):
    """Register existing file paths (no upload needed).

    Accepts paths to files already on disk — from other apps, folders, etc.
    Returns metadata (duration, resolution) for each valid file.
    """
    from core.ffmpeg_utils import probe_file
    body = await request.json()
    paths = body.get("paths", [])
    result = []
    for path in paths:
        p = Path(path)
        if p.is_dir():
            # Add all video files from folder
            for fp in sorted(p.iterdir()):
                if fp.suffix.lower() in VIDEO_EXTS:
                    info = probe_file(str(fp))
                    result.append({"path": str(fp), "name": fp.name, **info})
        elif p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            info = probe_file(str(p))
            result.append({"path": str(p), "name": p.name, **info})
        else:
            result.append({"path": path, "name": p.name, "error": "File not found or not a video"})
    return {"files": result}


@router.post("/upload")
async def upload_videos(files: list[UploadFile] = File(...)):
    saved = []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in VIDEO_EXTS:
            continue
        dest = UPLOADS_DIR / f"{uuid.uuid4().hex[:8]}_{f.filename}"
        data = await f.read()
        dest.write_bytes(data)
        from core.ffmpeg_utils import probe_file
        info = probe_file(str(dest))
        saved.append({
            "path": str(dest),
            "name": f.filename,
            "size": len(data),
            **info,
        })
    return {"files": saved}


@router.post("/process")
async def start_process(request: Request):
    from app import get_job_manager; job_manager = get_job_manager()

    body = await request.json()
    files = body.get("files", [])
    settings = body.get("settings", {})

    # Accept either list of paths or list of dicts with "path" key
    file_list = []
    for item in files:
        if isinstance(item, str):
            file_list.append(item)
        elif isinstance(item, dict) and "path" in item:
            file_list.append(item["path"])

    if not file_list:
        raise HTTPException(400, "No files provided")

    # Validate all files exist
    for path in file_list:
        if not os.path.isfile(path):
            raise HTTPException(400, f"File not found: {path}")

    # Merge with config defaults
    config = cfg.load()
    merged = {
        "crf": config.get("tools_crf", 18),
        "out_format": config.get("tools_out_format", "mp4"),
        "out_dir": config.get("tools_out_dir", "") or str(OUTPUT_DIR),
    }
    merged.update(settings)

    job = job_manager.submit(
        JOB_VIDEO_TOOL, process_batch, file_list, merged,
        label=f"{len(file_list)} files",
    )
    return {"job_id": job.id}


@router.post("/mix-music")
async def mix_music(request: Request):
    """Mix background music under a video."""
    from app import get_job_manager; job_manager = get_job_manager()
    from features.video_tools.music_mixer import mix_music_under_video

    body = await request.json()
    video_path = body.get("video_path", "")
    music_path = body.get("music_path", "")

    if not video_path or not os.path.isfile(video_path):
        raise HTTPException(400, "Video file not found")
    if not music_path or not os.path.isfile(music_path):
        raise HTTPException(400, "Music file not found")

    # Validate volume dB values — sane range is -60 to +20 dB
    settings = body.get("settings", {})
    try:
        music_vol = max(-60.0, min(20.0, float(settings.get("music_volume_db", -18.0))))
        dialogue_vol = max(-60.0, min(20.0, float(settings.get("dialogue_volume_db", 0.0))))
    except (ValueError, TypeError):
        raise HTTPException(422, "Volume values must be numbers between -60 and +20 dB")

    def _worker(job, vpath, mpath, m_vol, d_vol):
        job.update(progress=10, message="Mixing audio...")
        result = mix_music_under_video(
            vpath, mpath,
            music_volume_db=m_vol,
            dialogue_volume_db=d_vol,
        )
        if result["success"]:
            job.output = result["output_path"]
            job.message = "Music mixed successfully"
        else:
            raise RuntimeError(result["error"])

    job = job_manager.submit(
        JOB_VIDEO_TOOL, _worker, video_path, music_path, music_vol, dialogue_vol,
        label="Music mix",
    )
    return {"job_id": job.id}
