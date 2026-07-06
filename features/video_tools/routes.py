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


@router.get("/upscale")
async def upscale_info():
    """Report upscale capabilities so the UI can adapt (no install nagging)."""
    from core.upscaler import ai_available
    return {"ai_available": ai_available()}


@router.post("/upscale")
async def start_upscale(request: Request):
    """Batch upscale/optimize videos -- Lanczos (fast) or Real-ESRGAN (AI).

    Body:
        files    -- list of paths (or dicts with "path")
        settings -- scale (1.0/1.5/2.0/3.0/4.0), method ('ffmpeg'|'ai'),
                    crf (0-51), out_dir (optional override)
    """
    from app import get_job_manager; job_manager = get_job_manager()
    from features.video_tools.upscale_batch import process_upscale_batch

    body = await request.json()
    files = body.get("files", [])
    settings = body.get("settings", {})

    file_list = []
    for item in files:
        if isinstance(item, str):
            file_list.append(item)
        elif isinstance(item, dict) and "path" in item:
            file_list.append(item["path"])

    if not file_list:
        raise HTTPException(400, "No files provided")
    for path in file_list:
        if not os.path.isfile(path):
            raise HTTPException(400, f"File not found: {path}")

    try:
        scale = float(settings.get("scale", 2.0))
        crf = int(settings.get("crf", 18))
    except (ValueError, TypeError):
        raise HTTPException(422, "scale must be a number, crf an integer")
    if not 1.0 <= scale <= 4.0:
        raise HTTPException(422, "scale must be between 1.0 and 4.0")
    method = settings.get("method", "ffmpeg")
    if method not in ("ffmpeg", "ai"):
        raise HTTPException(422, "method must be 'ffmpeg' or 'ai'")

    config = cfg.load()
    merged = {
        "scale": scale,
        "method": method,
        "crf": max(0, min(51, crf)),
        "out_dir": settings.get("out_dir", "") or config.get("tools_out_dir", "") or str(OUTPUT_DIR),
    }

    job = job_manager.submit(
        JOB_VIDEO_TOOL, process_upscale_batch, file_list, merged,
        label=f"Upscale {len(file_list)} file(s)",
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


@router.post("/pipeline")
async def run_video_pipeline(request: Request):
    """Apply an ordered list of edit steps to one or more videos in a single run.

    Body:
        video_path  -- absolute path to a source video (single), OR
        video_paths -- list of source video paths (batch; 2 encode at once)
        steps       -- ordered list of {op, ...params}; op in
                       upscale | sharpen | crop | transform | smooth
        out_dir     -- optional output directory override
    """
    from app import get_job_manager; job_manager = get_job_manager()
    from features.video_tools.pipeline import VALID_OPS, run_pipeline, run_pipeline_batch

    body = await request.json()
    paths = body.get("video_paths")
    if not paths:
        single = body.get("video_path", "")
        paths = [single] if single else []
    steps = body.get("steps", [])
    out_dir = body.get("out_dir", "")

    if not isinstance(paths, list) or not paths:
        raise HTTPException(400, "No video provided")
    for p in paths:
        if not p or not os.path.isfile(p):
            raise HTTPException(400, f"Video file not found: {p}")
    if not isinstance(steps, list) or not steps:
        raise HTTPException(400, "Add at least one step")
    for s in steps:
        if not isinstance(s, dict) or s.get("op") not in VALID_OPS:
            bad = s.get("op") if isinstance(s, dict) else s
            raise HTTPException(400, f"Invalid step: {bad}")

    n_steps = len(steps)
    if len(paths) == 1:
        def _worker(job, sp, st, od):
            run_pipeline(job, sp[0], st, od)
        label = f"Edit {Path(paths[0]).name} ({n_steps} step{'s' if n_steps != 1 else ''})"
    else:
        def _worker(job, sp, st, od):
            run_pipeline_batch(job, sp, st, od)
        label = f"Edit {len(paths)} videos ({n_steps} step{'s' if n_steps != 1 else ''})"

    job = job_manager.submit(JOB_VIDEO_TOOL, _worker, paths, steps, out_dir, label=label)
    return {"job_id": job.id}


@router.get("/interpolate")
async def interpolate_info():
    """Report interpolation capabilities so the UI can adapt (no install nagging)."""
    from features.video_tools.interpolator import _find_rife
    return {"rife_available": _find_rife() is not None}


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
    if mode not in ("auto", "blend", "mci", "rife"):
        raise HTTPException(400, "mode must be auto, blend, mci, or rife")

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


# -- Crop / Reframe -----------------------------------------------------------

def _extract_crop_frame_b64(video_path: str, seek_sec: float, max_dim: int = 960) -> str | None:
    """Extract one frame at an absolute timestamp as a base64 JPEG (no data: prefix)."""
    import base64
    import subprocess
    try:
        r = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", f"{max(0.0, seek_sec):.3f}",
                "-i", str(video_path),
                "-frames:v", "1",
                "-vf", f"scale='min({max_dim},iw)':'-2'",
                "-f", "image2", "-c:v", "mjpeg", "-q:v", "3",
                "pipe:1",
            ],
            capture_output=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout:
            return base64.b64encode(r.stdout).decode()
    except Exception as e:
        log.debug("_extract_crop_frame_b64(%s @ %.2fs) failed: %s", video_path, seek_sec, e)
    return None


@router.post("/crop-frames")
async def crop_frames(request: Request):
    """Return 3 preview frames sampled from the first 30s of a video, plus its
    native dimensions, so the UI can draw a crop marquee against real footage.

    Body: { video_path }
    Returns: { width, height, duration, frames: [{ t, b64 }] }
    """
    from core.ffmpeg_utils import probe_file

    body = await request.json()
    video_path = body.get("video_path", "")
    if not video_path or not os.path.isfile(video_path):
        raise HTTPException(400, "Video file not found")

    def _work():
        info = probe_file(video_path)
        w, h = info.get("width"), info.get("height")
        dur = float(info.get("duration") or 0.0)
        if not w or not h:
            raise HTTPException(422, "Could not read video dimensions")

        # Sample within the first 30s (or the whole clip if shorter), avoiding
        # the very first/last frames which are often black or motion-blurred.
        window = min(30.0, dur) if dur > 0 else 30.0
        fracs = [0.12, 0.5, 0.88]
        frames = []
        for fr in fracs:
            t = fr * window
            if dur > 0:
                t = max(0.1, min(t, dur - 0.05))
            b64 = _extract_crop_frame_b64(video_path, t)
            if b64:
                frames.append({"t": round(t, 2), "b64": b64})
        if not frames:
            raise HTTPException(500, "Could not extract preview frames -- is ffmpeg installed?")
        return {"width": int(w), "height": int(h), "duration": round(dur, 2), "frames": frames}

    return await asyncio.to_thread(_work)


@router.post("/crop")
async def start_crop(request: Request):
    """Crop a single video to a rectangle chosen in the UI, re-encoding the
    whole clip. The rect is given in normalized 0..1 coordinates of the source
    frame so it is resolution-independent.

    Body:
        video_path -- absolute path to the source video
        rect       -- { x, y, w, h } as fractions (0..1) of source width/height
        keep_audio -- bool (default True)
    """
    from app import get_job_manager; job_manager = get_job_manager()
    from core.ffmpeg_utils import probe_file, round_even, video_encode_args
    import datetime
    import subprocess

    body = await request.json()
    video_path = body.get("video_path", "")
    rect = body.get("rect", {}) or {}
    keep_audio = bool(body.get("keep_audio", True))

    if not video_path or not os.path.isfile(video_path):
        raise HTTPException(400, "Video file not found")
    try:
        rx = float(rect["x"]); ry = float(rect["y"])
        rw = float(rect["w"]); rh = float(rect["h"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(422, "rect must have numeric x, y, w, h")
    if not (rw > 0 and rh > 0):
        raise HTTPException(422, "crop width/height must be positive")

    config = cfg.load()
    out_dir = body.get("out_dir", "") or config.get("tools_out_dir", "") or str(OUTPUT_DIR)
    crf = int(config.get("tools_crf", 18))

    def _worker(job, src, r, keep_aud, base_out_dir, crf_val):
        info = probe_file(src)
        W, H = info.get("width"), info.get("height")
        has_audio = info.get("has_audio", False)
        dur = float(info.get("duration") or 0.0)
        if not W or not H:
            raise RuntimeError("Could not read video dimensions")

        # Normalized rect -> even source pixels, clamped inside the frame.
        cw = max(2, min(round_even(r["w"] * W), W - (W % 2)))
        ch = max(2, min(round_even(r["h"] * H), H - (H % 2)))
        cx = int(round(r["x"] * W)); cy = int(round(r["y"] * H))
        cx = max(0, min(cx, W - cw)); cy = max(0, min(cy, H - ch))

        src_p = Path(src)
        date_str = datetime.date.today().strftime("%Y-%m-%d")
        dest_dir = Path(base_out_dir) / date_str
        dest_dir.mkdir(parents=True, exist_ok=True)
        dst = dest_dir / f"{src_p.stem}_cropped_{cw}x{ch}.mp4"

        job.update(progress=8, message=f"Cropping to {cw}x{ch}...")

        cmd = [
            "ffmpeg", "-y", "-i", src,
            "-vf", f"crop={cw}:{ch}:{cx}:{cy}",
            *video_encode_args(crf=int(crf_val)),
        ]
        if keep_aud and has_audio:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-an"]
        cmd += ["-progress", "pipe:1", "-nostats", str(dst)]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            for line in proc.stdout or []:
                if job.stop_event.is_set():
                    proc.kill()
                    return
                if line.startswith("out_time=") and dur > 0:
                    ts = line.strip().split("=", 1)[1]
                    try:
                        hh, mm, ss = ts.split(":")
                        secs = int(hh) * 3600 + int(mm) * 60 + float(ss)
                        job.update(progress=min(95, 8 + int(secs / dur * 87)),
                                   message=f"Cropping to {cw}x{ch}...")
                    except (ValueError, ZeroDivisionError):
                        pass
        finally:
            proc.wait()

        if job.stop_event.is_set():
            return
        if proc.returncode != 0 or not dst.exists():
            raise RuntimeError("ffmpeg crop failed")

        job.output = str(dst)
        job.meta["outputs"] = [str(dst)]
        job.meta["crop"] = {"x": cx, "y": cy, "w": cw, "h": ch}
        job.message = f"Cropped to {cw}x{ch}"
        try:
            from core.inbox import copy_to_inbox; copy_to_inbox(str(dst))
        except Exception:
            pass
        from core.session import get_current as get_session
        get_session().add_file(dst.name, "video", "video_tools", path=str(dst))

    job = job_manager.submit(
        JOB_VIDEO_TOOL, _worker, video_path,
        {"x": rx, "y": ry, "w": rw, "h": rh}, keep_audio, out_dir, crf,
        label=f"Crop {Path(video_path).name}",
    )
    return {"job_id": job.id}
