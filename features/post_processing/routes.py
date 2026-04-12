"""Post Processing routes — Netflix VOID video inpainting.

Provides endpoints for the Post Processing tab:
  POST /api/post/inpaint       — Submit a video + mask for VOID inpainting (job-based)
  POST /api/post/extract-frame — Extract a frame for mask painting
  GET  /api/post/status        — VOID worker status (pass-through to void_worker)
  GET  /api/post/void-ready    — Whether VOID model is downloaded and worker is up
"""

import logging
import os
import time
import json
import urllib.request
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.responses import JSONResponse

from app import get_job_manager
from core import config as cfg
from core import session
from core.ffmpeg_utils import extract_frame_b64

log = logging.getLogger(__name__)
router = APIRouter()

VOID_WORKER_PORT = 7901
VOID_WORKER_URL  = f"http://127.0.0.1:{VOID_WORKER_PORT}"

APP_DIR    = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = APP_DIR / "output"
UPLOADS_DIR = APP_DIR / "uploads"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _void_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{VOID_WORKER_URL}/health", timeout=2):
            return True
    except Exception:
        return False


def _void_post(endpoint: str, payload: dict, timeout: int = 10) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{VOID_WORKER_URL}{endpoint}",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/void-ready")
async def void_ready():
    """Check if the VOID worker is up and model is loaded."""
    alive = _void_alive()
    model_dir = cfg.get("void_model_dir") or ""
    configured = bool(model_dir)
    return {
        "alive": alive,
        "configured": configured,
        "model_dir": model_dir,
    }


@router.post("/upload")
async def upload_video(file: UploadFile = File(...)):
    """Upload a video file for inpainting."""
    ext = Path(file.filename).suffix.lower()
    if ext not in (".mp4", ".mov", ".avi", ".webm", ".mkv"):
        raise HTTPException(status_code=400, detail="Unsupported video format")

    safe_name = f"void_upload_{int(time.time() * 1000)}{ext}"
    dest = UPLOADS_DIR / safe_name
    dest.parent.mkdir(exist_ok=True)

    size_limit = 2 * 1024 ** 3  # 2 GB
    if file.size is not None and file.size > size_limit:
        raise HTTPException(status_code=413, detail="File too large (max 2 GB)")

    content = await file.read()
    if len(content) > size_limit:
        raise HTTPException(status_code=413, detail="File too large (max 2 GB)")
    dest.write_bytes(content)
    log.info("VOID upload: %s (%d bytes)", safe_name, len(content))

    return {"filename": safe_name, "path": str(dest), "url": f"/uploads/{safe_name}"}


@router.post("/extract-frame")
async def extract_frame(request: Request):
    """Extract a video frame at a given position for mask painting.

    Body: { path: str, position: float }
    Returns: { frame_b64: str, width: int, height: int }
    """
    import base64
    body = await request.json()
    path = body.get("path", "")
    position = float(body.get("position", 0.0))

    vid = Path(path) if os.path.isabs(path) else UPLOADS_DIR / path
    if not vid.exists():
        raise HTTPException(status_code=404, detail="Video not found")

    b64 = extract_frame_b64(str(vid), position=position)
    if not b64:
        raise HTTPException(status_code=500, detail="Frame extraction failed")

    return {"frame_b64": b64}


@router.post("/inpaint")
async def inpaint(request: Request):
    """Submit a video inpainting job via Netflix VOID.

    Body:
        video_path   : str   — path to input video (relative to uploads/ or absolute)
        mask_b64     : str   — PNG mask image (base64) painted by the user.
                               White (255) = remove, Black (0) = keep.
        prompt       : str   — optional text hint (e.g. "empty room")
        steps        : int   — inference steps (default 30)
        seed         : int   — random seed (-1 = random)

    The mask is a single static frame mask that VOID propagates through the video.
    Returns: { job_id: str }
    """
    body = await request.json()
    video_path = body.get("video_path", "")
    mask_b64   = body.get("mask_b64", "")
    prompt     = body.get("prompt", "")
    steps      = int(body.get("steps", 30))
    seed       = int(body.get("seed", -1))

    if not video_path:
        raise HTTPException(status_code=400, detail="video_path required")
    if not mask_b64:
        raise HTTPException(status_code=400, detail="mask_b64 required")

    vid = Path(video_path) if os.path.isabs(video_path) else UPLOADS_DIR / video_path
    if not vid.exists():
        raise HTTPException(status_code=404, detail="Video not found")

    jm = get_job_manager()
    job_dir = OUTPUT_DIR / time.strftime("%Y-%m-%d") / f"void_{int(time.time() * 1000)}"
    job_dir.mkdir(parents=True, exist_ok=True)

    def _run(job):
        if not _void_alive():
            job.error = "VOID worker not running -- start it in the Services tab"
            return

        output_path = str(job_dir / "inpainted.mp4")
        try:
            _void_post("/inpaint", {
                "video_path":  str(vid),
                "mask_b64":    mask_b64,
                "output_path": output_path,
                "prompt":      prompt,
                "steps":       steps,
                "seed":        seed,
            }, timeout=30)
        except Exception as e:
            job.error = str(e)
            return

        # VOID /inpaint fires a background thread and returns immediately.
        # Poll /status until busy == False before marking the job done.
        deadline = time.time() + 1800  # 30 min max
        while time.time() < deadline:
            if job.stop_event.is_set():
                job.error = "Cancelled"
                return
            try:
                with urllib.request.urlopen(f"{VOID_WORKER_URL}/status", timeout=5) as r:
                    s = json.loads(r.read())
            except Exception:
                time.sleep(3)
                continue
            if not s.get("busy"):
                if s.get("result"):
                    out = s["result"]
                    try:
                        rel = Path(out).relative_to(OUTPUT_DIR).as_posix()
                        url = f"/output/{rel}"
                    except ValueError:
                        url = f"/output/{Path(out).name}"
                    session.get_current().add_file(
                        filename=out,
                        kind="video",
                        source="post_processing",
                        label="VOID inpaint",
                    )
                    job.output = {"url": url, "path": out}
                else:
                    job.error = s.get("error") or "VOID inpainting failed"
                return
            job.message = s.get("progress", "Running...")
            time.sleep(2)
        job.error = "Timed out after 30 minutes"

    job = jm.submit("void_inpaint", _run, label="VOID Inpaint")
    return {"job_id": job.id}


@router.get("/status")
async def void_status():
    """Pass-through to the VOID worker's status endpoint."""
    if not _void_alive():
        return {"alive": False, "busy": False, "progress": "Worker not running"}
    try:
        with urllib.request.urlopen(f"{VOID_WORKER_URL}/status", timeout=3) as r:
            data = json.loads(r.read())
            data["alive"] = True
            return data
    except Exception:
        return {"alive": False, "busy": False, "progress": ""}
