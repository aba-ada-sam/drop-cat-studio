"""Manual video-retime API routes -- /api/retime

Warps a finished video's timeline to user-placed lock points and re-muxes the
original audio. Fully manual; no beat/peak detection. Used by the Stretch & Lock
tool in the Queue modal and the Gallery detail view.
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


@router.post("/run")
async def run_retime(request: Request):
    """Body: { video_path, anchors: [{src_t, dst_t}, ...] }.

    Submits a non-GPU job that retimes the video. Returns {job_id}.
    """
    from app import get_job_manager, gallery_push
    from features.retime.retimer import retime_video

    body = await request.json()
    video_path = body.get("video_path", "")
    anchors = body.get("anchors", []) or []

    if not video_path or not os.path.isfile(video_path):
        raise HTTPException(400, "Video file not found")
    if Path(video_path).suffix.lower() not in _VIDEO_EXTS:
        raise HTTPException(400, "Not a video file")
    if not isinstance(anchors, list):
        raise HTTPException(422, "anchors must be a list")

    # Sanitize anchors to numeric pairs.
    clean = []
    for a in anchors:
        try:
            clean.append({"src_t": float(a["src_t"]), "dst_t": float(a["dst_t"])})
        except (KeyError, TypeError, ValueError):
            continue

    src = Path(video_path)
    date_str = datetime.date.today().strftime("%Y-%m-%d")
    dest_dir = OUTPUT_DIR / date_str
    dest_dir.mkdir(parents=True, exist_ok=True)
    dst = dest_dir / f"{src.stem}_retimed_{datetime.datetime.now().strftime('%H%M%S')}.mp4"

    def _worker(job, in_path, out_path, anchor_list):
        job.update(progress=5, message="Starting retime...")
        retime_video(job, in_path, out_path, anchor_list)
        job.output = out_path
        job.meta["final_path"] = out_path
        try:
            from core.session import get_current as get_session
            get_session().add_file(Path(out_path).name, "video", "retime", path=out_path)
        except Exception as e:
            log.warning("[retime] session add_file failed: %s", e)
        # Surface in the gallery like other generations.
        norm = out_path.replace("\\", "/")
        i = norm.lower().find("/output/")
        url = norm[i:] if i != -1 else f"/output/{Path(out_path).name}"
        gallery_push(url, tab="retime", prompt="Stretch & Lock retime",
                     metadata={"path": out_path, "source": in_path})
        from core.inbox import copy_to_inbox
        copy_to_inbox(out_path)
        job.update(status="done", progress=100, message=f"Retimed: {Path(out_path).name}")

    job_manager = get_job_manager()
    from core.job_manager import JOB_VIDEO_TOOL
    job = job_manager.submit(
        JOB_VIDEO_TOOL, _worker, str(src), str(dst), clean,
        label=f"Retime {src.name}",
    )
    return {"job_id": job.id}
