"""Video Tools API routes -- /api/tools/*

Batch video transforms: reverse, mirror, flip, speed, upscale, sharpen.
Also includes the music mixer from Github Video Editor.
"""
import asyncio
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

    Accepts paths to files already on disk -- from other apps, folders, etc.
    Returns metadata (duration, resolution) for each valid file.
    """
    from core.ffmpeg_utils import probe_file
    body = await request.json()
    paths = body.get("paths", [])

    def _scan():
        result = []
        for path in paths:
            p = Path(path)
            if p.is_dir():
                for fp in sorted(p.iterdir()):
                    if fp.suffix.lower() in VIDEO_EXTS:
                        info = probe_file(str(fp))
                        result.append({"path": str(fp), "name": fp.name, **info})
            elif p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                info = probe_file(str(p))
                result.append({"path": str(p), "name": p.name, **info})
            else:
                result.append({"path": path, "name": p.name, "error": "File not found or not a video"})
        return result

    return {"files": await asyncio.to_thread(_scan)}


@router.post("/upload")
async def upload_videos(files: list[UploadFile] = File(...)):
    saved = []
    rejected = []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in VIDEO_EXTS:
            rejected.append(f.filename or "unnamed")
            continue
        dest = UPLOADS_DIR / f"{uuid.uuid4().hex[:8]}_{f.filename}"
        data = await f.read()
        await asyncio.to_thread(dest.write_bytes, data)
        from core.ffmpeg_utils import probe_file
        info = await asyncio.to_thread(probe_file, str(dest))
        saved.append({
            "path": str(dest),
            "name": f.filename,
            "size": len(data),
            **info,
        })
    return {"files": saved, "rejected": rejected}


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

    # Validate volume dB values -- sane range is -60 to +20 dB
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
            from core.inbox import copy_to_inbox; copy_to_inbox(job.output)
            job.message = "Music mixed successfully"
        else:
            raise RuntimeError(result["error"])

    job = job_manager.submit(
        JOB_VIDEO_TOOL, _worker, video_path, music_path, music_vol, dialogue_vol,
        label="Music mix",
    )
    return {"job_id": job.id}


@router.post("/interpolate")
async def interpolate_frames(request: Request):
    """Smooth jerky footage by generating in-between frames.

    Body:
        video_path  -- absolute path to the source video
        target_fps  -- desired output frame rate (default: 2x source)
        mode        -- "blend" (fast), "mci" (quality), "rife" (best, needs RIFE exe)
        out_dir     -- optional output directory override
    """
    from app import get_job_manager; job_manager = get_job_manager()
    from features.video_tools.interpolator import interpolate_video
    import datetime

    body = await request.json()
    video_path = body.get("video_path", "")
    target_fps = float(body.get("target_fps", 0))
    mode = body.get("mode", "blend")
    out_dir = body.get("out_dir", "")

    if not video_path or not os.path.isfile(video_path):
        raise HTTPException(400, "Video file not found")
    if mode not in ("blend", "mci", "rife"):
        raise HTTPException(400, "mode must be blend, mci, or rife")

    src = Path(video_path)
    date_str = datetime.date.today().strftime("%Y-%m-%d")
    dest_dir = Path(out_dir) if out_dir else OUTPUT_DIR / date_str
    dest_dir.mkdir(parents=True, exist_ok=True)
    dst = dest_dir / f"{src.stem}_smooth_{mode}{src.suffix}"

    def _worker(job, s, d, fps, m):
        interpolate_video(job, s, d, fps, m)
        job.output = str(d)
        from core.session import get_current as get_session
        get_session().add_file(d.name, "video", "video_tools", path=str(d))
        job.update(status="done", progress=100, message=f"Saved: {d.name}")

    job = job_manager.submit(
        JOB_VIDEO_TOOL, _worker, str(src), str(dst), target_fps, mode,
        label=f"Interpolate {src.name}",
    )
    return {"job_id": job.id}
