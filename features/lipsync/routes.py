"""Lip-sync API routes -- /api/lipsync

Mouth-syncs an existing video to a driving audio via MuseTalk (post-pass).
Manual, on-demand. Needs a detectable frontal face in the input video.
"""
import datetime
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

log = logging.getLogger(__name__)
router = APIRouter()

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}


@router.get("/status")
async def lipsync_status():
    """Report whether MuseTalk is installed/usable (UI shows the tool only then)."""
    from features.lipsync.runner import lipsync_available
    return {"available": lipsync_available()}


@router.post("/run")
async def run_lipsync(request: Request):
    """Body: { video_path, audio_path?, bbox_shift? }.

    audio_path optional -- if omitted, the video's own audio drives the sync.
    Returns {job_id}.
    """
    from app import get_job_manager, gallery_push
    from features.lipsync.runner import lipsync_video, lipsync_available

    if not lipsync_available():
        raise HTTPException(503, "MuseTalk lip-sync is not installed on this machine")

    body = await request.json()
    video_path = body.get("video_path", "")
    audio_path = body.get("audio_path") or None
    try:
        bbox_shift = int(body.get("bbox_shift", 0))
    except (TypeError, ValueError):
        bbox_shift = 0

    if not video_path or not os.path.isfile(video_path):
        raise HTTPException(400, "Video file not found")
    if Path(video_path).suffix.lower() not in _VIDEO_EXTS:
        raise HTTPException(400, "Not a video file")
    if audio_path and not os.path.isfile(audio_path):
        raise HTTPException(400, "Audio file not found")

    src = Path(video_path)
    date_str = datetime.date.today().strftime("%Y-%m-%d")
    dest_dir = OUTPUT_DIR / date_str
    dest_dir.mkdir(parents=True, exist_ok=True)
    dst = dest_dir / f"{src.stem}_lipsync_{datetime.datetime.now().strftime('%H%M%S')}.mp4"

    def _worker(job, vpath, apath, out_path, shift):
        job.update(progress=4, message="Starting lip-sync...")
        lipsync_video(job, vpath, apath, out_path, bbox_shift=shift)
        job.output = out_path
        job.meta["final_path"] = out_path
        try:
            from core.session import get_current as get_session
            get_session().add_file(Path(out_path).name, "video", "lipsync", path=out_path)
        except Exception as e:
            log.warning("[lipsync] session add_file failed: %s", e)
        norm = out_path.replace("\\", "/")
        i = norm.lower().find("/output/")
        url = norm[i:] if i != -1 else f"/output/{Path(out_path).name}"
        gallery_push(url, tab="lipsync", prompt="MuseTalk lip-sync",
                     metadata={"path": out_path, "source": vpath})
        try:
            from core.inbox import copy_to_inbox
            copy_to_inbox(out_path)
        except Exception:
            pass
        job.update(status="done", progress=100, message=f"Lip-synced: {Path(out_path).name}")

    job_manager = get_job_manager()
    from core.job_manager import JOB_VIDEO_TOOL
    job = job_manager.submit(
        JOB_VIDEO_TOOL, _worker, str(src), audio_path, str(dst), bbox_shift,
        label=f"Lip-sync {src.name}",
    )
    return {"job_id": job.id}
