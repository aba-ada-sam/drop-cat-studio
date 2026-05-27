"""Beat-sync API routes: analyze audio/video peaks, retime video to audio."""
import logging
import os
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from core.job_manager import JOB_VIDEO_TOOL
from core import session as _session

router = APIRouter()
log = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"
_SERVE_ROOTS = None

def _allowed_path(p: str) -> bool:
    """Only serve files under output/ or uploads/ to prevent path traversal."""
    global _SERVE_ROOTS
    if _SERVE_ROOTS is None:
        base = Path(__file__).resolve().parent.parent.parent
        _SERVE_ROOTS = [base / "output", base / "uploads"]
    resolved = Path(p).resolve()
    return any(resolved.is_relative_to(r) for r in _SERVE_ROOTS)


@router.get("/serve")
async def serve_file(path: str):
    """Serve an output/upload file to the browser (for Web Audio API decoding)."""
    if not path or not os.path.isfile(path) or not _allowed_path(path):
        raise HTTPException(404, "File not found")
    return FileResponse(path, media_type="application/octet-stream")


@router.post("/extract-audio")
async def extract_audio(body: dict):
    """Extract audio track from an existing MP4 into a WAV file for analysis."""
    import asyncio, subprocess, time
    video_path = body.get("video_path", "")
    if not video_path or not os.path.isfile(video_path):
        return {"error": "video_path not found"}

    ts = time.strftime("%Y-%m-%d")
    out_dir = OUTPUT_DIR / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(video_path).stem[:24]
    audio_out = str(out_dir / f"{stem}_audio_{int(time.time())}.wav")

    def _extract():
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", video_path,
             "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
             audio_out],
            capture_output=True, timeout=120,
        )
        if r.returncode != 0 or not Path(audio_out).exists():
            return None, r.stderr.decode(errors="replace")[-400:]
        return audio_out, None

    path, err = await asyncio.to_thread(_extract)
    if not path:
        return {"error": err or "Audio extraction failed"}
    return {"audio_path": path}


@router.post("/analyze")
async def analyze_sync(body: dict):
    """Analyze audio beats and video motion peaks for alignment UI."""
    import asyncio
    from features.beat_sync.analyzer import analyze_audio_beats, analyze_video_motion
    audio_path = body.get("audio_path", "")
    video_path = body.get("video_path", "")
    result = {}
    if audio_path and os.path.isfile(audio_path):
        result["audio"] = await asyncio.to_thread(analyze_audio_beats, audio_path)
    if video_path and os.path.isfile(video_path):
        result["video"] = await asyncio.to_thread(analyze_video_motion, video_path)
    return result


@router.post("/retime")
async def retime_video(body: dict):
    """Submit a beat-sync retime job. Returns {job_id}."""
    video_path   = body.get("video_path", "")
    audio_path   = body.get("audio_path", "")
    remap_points = body.get("remap_points")   # None = auto-align

    if not video_path or not os.path.isfile(video_path):
        return {"error": "video_path not found"}
    if not audio_path or not os.path.isfile(audio_path):
        return {"error": "audio_path not found"}

    ts = time.strftime("%Y-%m-%d")
    out_dir = OUTPUT_DIR / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(video_path).stem[:20]
    out_path = str(out_dir / f"{stem}_synced_{int(time.time())}.mp4")

    def _work(job, vp=video_path, ap=audio_path, op=out_path, rp=remap_points):
        job.update(progress=5, message="Analyzing audio and video peaks...")
        from features.beat_sync.retimer import retime_video as _retime
        ok, err = _retime(vp, ap, op, remap_points=rp, auto_align=(rp is None))
        if ok:
            job.update(status="done", progress=100, output=op)
            _session.get_current().add_file(Path(op).name, "video", "beat_sync", path=op)
        else:
            raise RuntimeError(err or "Retime failed")

    from app import get_job_manager
    job = get_job_manager().submit(JOB_VIDEO_TOOL, _work, label="Beat sync retime")
    return {"job_id": job.id}
