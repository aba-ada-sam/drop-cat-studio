"""Video Bridges API routes — /api/bridges/*

Generate AI-powered transition videos between clips.
"""
import logging
import os
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from core import config as cfg
from core.ffmpeg_utils import extract_frame_b64, probe_file
from core.job_manager import JOB_BRIDGE

log = logging.getLogger(__name__)
router = APIRouter()

UPLOADS_DIR = Path(__file__).resolve().parent.parent.parent / "uploads"
OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"
MEDIA_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".jpg", ".jpeg", ".png", ".webp", ".bmp"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


@router.post("/add-paths")
async def add_paths(request: Request):
    """Register existing file paths without uploading.

    Accepts video files, image files, or folders. Returns metadata for each.
    """
    from core.ffmpeg_utils import probe_file
    body = await request.json()
    paths = body.get("paths", [])
    result = []
    for path in paths:
        p = Path(path)
        if p.is_dir():
            for fp in sorted(p.iterdir()):
                if fp.suffix.lower() in MEDIA_EXTS:
                    info = probe_file(str(fp))
                    kind = "image" if fp.suffix.lower() in IMAGE_EXTS else "video"
                    result.append({"path": str(fp), "name": fp.name, "kind": kind, **info})
        elif p.is_file():
            if p.suffix.lower() in MEDIA_EXTS:
                info = probe_file(str(p))
                kind = "image" if p.suffix.lower() in IMAGE_EXTS else "video"
                result.append({"path": str(p), "name": p.name, "kind": kind, **info})
            else:
                result.append({"path": path, "name": p.name, "error": "Unsupported file type"})
        else:
            result.append({"path": path, "name": p.name, "error": "Path not found"})
    return {"files": result}


@router.post("/upload")
async def upload_media(files: list[UploadFile] = File(...)):
    saved = []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in MEDIA_EXTS:
            continue
        dest = UPLOADS_DIR / f"{uuid.uuid4().hex[:8]}_{f.filename}"
        data = await f.read()
        dest.write_bytes(data)
        info = probe_file(str(dest))
        kind = "image" if ext in IMAGE_EXTS else "video"
        saved.append({
            "path": str(dest),
            "name": f.filename,
            "kind": kind,
            "size": len(data),
            **info,
        })
    return {"files": saved}


@router.post("/analyze")
async def analyze_media_endpoint(request: Request):
    from app import get_llm_router; llm_router = get_llm_router()
    from features.video_bridges.analyzer import analyze_media

    body = await request.json()
    path = body.get("path", "")
    if not path or not os.path.isfile(path):
        raise HTTPException(400, "File not found")
    return analyze_media(llm_router, path)


@router.post("/bridge-preview")
async def bridge_preview(request: Request):
    """Preview a bridge prompt for a specific pair (no generation)."""
    from app import get_llm_router; llm_router = get_llm_router()
    from features.video_bridges.analyzer import generate_bridge_prompt

    body = await request.json()
    analysis_a = body.get("analysis_a", {})
    analysis_b = body.get("analysis_b", {})
    path_a = body.get("path_a", "")
    path_b = body.get("path_b", "")

    config = cfg.load()
    frame_a = extract_frame_b64(path_a, position=0.97) if path_a else None
    frame_b = extract_frame_b64(path_b, position=0.03) if path_b else None

    prompt = generate_bridge_prompt(
        llm_router,
        analysis_a, analysis_b,
        frame_a, frame_b,
        transition_mode=body.get("transition_mode", config.get("bridge_transition_mode", "cinematic")),
        prompt_mode=body.get("prompt_mode", config.get("bridge_prompt_mode", "ai_informed")),
        creativity=float(body.get("creativity", config.get("bridge_creativity", 9.0))),
        user_guidance=body.get("user_guidance", ""),
    )
    return {"prompt": prompt}


def _bridges_worker(job, items, settings):
    """Background worker for bridge generation."""
    from app import get_llm_router; llm_router = get_llm_router()
    from features.video_bridges.analyzer import analyze_media, generate_bridge_prompt
    from features.video_bridges.bridge_generator import generate_bridge, compile_with_bridges

    ts = time.strftime("%Y-%m-%d")
    first_path = next((it["path"] for it in items if it.get("path")), None)
    slug = Path(first_path).stem[:15].replace(" ", "_") if first_path else "bridge"
    job_dir = OUTPUT_DIR / ts / f"{slug}_{job.id}"
    job_dir.mkdir(parents=True, exist_ok=True)

    segment_paths = [item.get("path") for item in items]
    segment_kinds = [item.get("kind", "video") for item in items]
    analyses = [item.get("analysis") for item in items]

    def _log(msg):
        log.info(msg)
    def _stopped():
        return job.stop_event.is_set()

    n = len(items)

    # ── Pre-generate any text-to-video clips ──────────────────────────────
    from features.fun_videos.video_generator import generate_video as _gen_video
    for i, item in enumerate(items):
        if item.get("kind") != "text":
            continue
        if _stopped():
            return
        job.update(progress=int(i / n * 15), message=f"Generating text clip {i+1}/{n}...")
        t2v_out = str(job_dir / f"t2v_{i:02d}.mp4")
        result = _gen_video(
            image_path=None,
            prompt=item.get("prompt", ""),
            out_path=t2v_out,
            duration=float(settings.get("image_duration", 4.0)),
            model_name="Wan2.1-T2V-14B",
            resolution=settings.get("resolution", "480p"),
            steps=int(settings.get("steps", 20)),
            guidance=float(settings.get("guidance", 10.0)),
            seed=-1,
            stop_check=_stopped,
            log_fn=_log,
        )
        if not result:
            raise RuntimeError(f"Text-to-video generation failed for clip {i + 1}")
        segment_paths[i] = result
        segment_kinds[i] = "video"

    # Analyze any missing clips (skip text clips — they have no original media to analyze)
    for i, analysis in enumerate(analyses):
        if _stopped():
            return
        if not analysis and segment_kinds[i] != "text":
            job.update(progress=15 + int(i / n * 10), message=f"Analyzing clip {i+1}/{n}...")
            analyses[i] = analyze_media(llm_router, segment_paths[i])

    bridge_paths = []
    num_bridges = n - 1

    for i in range(num_bridges):
        if _stopped():
            return

        pct = 20 + int(i / num_bridges * 60)
        job.update(progress=pct, message=f"Generating bridge {i+1}/{num_bridges}...")

        # Extract boundary frames
        start_pos = float(settings.get("start_frame_pos", 0.97))
        end_pos = float(settings.get("end_frame_pos", 0.03))
        frame_a = extract_frame_b64(segment_paths[i], position=start_pos)
        frame_b = extract_frame_b64(segment_paths[i + 1], position=end_pos)

        # Save boundary frames for reference
        from core.ffmpeg_utils import find_ffmpeg
        frame_a_path = str(job_dir / f"frame_end_{i:02d}.jpg")
        frame_b_path = str(job_dir / f"frame_start_{i+1:02d}.jpg")

        # Save frames as JPEG for WanGP input
        import base64 as _b64
        if frame_a:
            try:
                with open(frame_a_path, "wb") as f:
                    f.write(_b64.b64decode(frame_a))
            except Exception as _e:
                log.warning("Failed to save frame_a for bridge %d: %s", i, _e)
                frame_a_path = ""
        if frame_b:
            try:
                with open(frame_b_path, "wb") as f:
                    f.write(_b64.b64decode(frame_b))
            except Exception as _e:
                log.warning("Failed to save frame_b for bridge %d: %s", i, _e)
                frame_b_path = ""

        # Check for manual override
        overrides = settings.get("bridge_overrides", [])
        manual_prompt = overrides[i] if i < len(overrides) and overrides[i] else None

        if manual_prompt:
            prompt = manual_prompt
        else:
            prompt = generate_bridge_prompt(
                llm_router,
                analyses[i] or {}, analyses[i + 1] or {},
                frame_a, frame_b,
                transition_mode=settings.get("transition_mode", "cinematic"),
                prompt_mode=settings.get("prompt_mode", "ai_informed"),
                creativity=float(settings.get("creativity", 9.0)),
                user_guidance=settings.get("prompt_guidance", ""),
            )

        bridge_out = str(job_dir / f"bridge_{i:02d}_{i+1:02d}.mp4")
        bridge_path = generate_bridge(
            frame_a_path=frame_a_path if os.path.isfile(frame_a_path) else "",
            frame_b_path=frame_b_path if os.path.isfile(frame_b_path) else "",
            prompt=prompt,
            out_path=bridge_out,
            duration=float(settings.get("duration", 10.0)),
            model_name=settings.get("model", "LTX-2 Dev19B Distilled"),
            resolution=settings.get("resolution", "480p"),
            steps=int(settings.get("steps", 20)),
            guidance=float(settings.get("guidance", 10.0)),
            seed=int(settings.get("seed", -1)),
            use_end_frame=settings.get("use_end_frame", True),
            allow_fallback=settings.get("allow_bridge_fallback", True),
            stop_check=_stopped,
            log_fn=_log,
        )
        bridge_paths.append(bridge_path)

    if _stopped():
        return

    # Compile final video
    job.update(progress=85, message="Compiling final video...")
    model_tag = settings.get("model", "ltx2").split()[0].lower()
    final_out = str(job_dir / f"bridges_{model_tag}_{time.strftime('%H%M%S')}.mp4")

    result = compile_with_bridges(
        segment_paths=segment_paths,
        bridge_paths=bridge_paths,
        out_path=final_out,
        resolution=settings.get("resolution", "480p"),
        segment_kinds=segment_kinds,
        image_duration=float(settings.get("image_duration", 2.5)),
        log_fn=_log,
    )

    if result:
        job.output = result
        job.message = f"Complete! {num_bridges} bridge(s) generated"
        from core.session import get_current as get_session
        get_session().add_file(Path(result).name, "video", "video_bridges", path=result)
    else:
        raise RuntimeError("Final compilation failed")


@router.post("/generate")
async def start_generation(request: Request):
    from app import get_job_manager; job_manager = get_job_manager()

    body = await request.json()
    items = body.get("items", [])
    settings = body.get("settings", {})

    if len(items) < 2:
        raise HTTPException(400, "Need at least 2 clips")

    for item in items:
        if item.get("kind") == "text":
            if not item.get("prompt", "").strip():
                raise HTTPException(400, "Text clip is missing a prompt")
        elif not item.get("path") or not os.path.isfile(item["path"]):
            raise HTTPException(400, f"File not found: {item.get('path', '(none)')}")

    config = cfg.load()
    merged = {
        "model": config.get("wan_model", "LTX-2 Dev19B Distilled"),
        "resolution": config.get("resolution", "480p"),
        "duration": config.get("bridge_duration", 10.0),
        "steps": config.get("bridge_steps", 20),
        "guidance": config.get("bridge_guidance", 10.0),
        "creativity": config.get("bridge_creativity", 9.0),
        "transition_mode": config.get("bridge_transition_mode", "cinematic"),
        "prompt_mode": config.get("bridge_prompt_mode", "ai_informed"),
        "prompt_guidance": config.get("bridge_prompt_guidance", ""),
        "start_frame_pos": config.get("bridge_start_frame_pos", 0.97),
        "end_frame_pos": config.get("bridge_end_frame_pos", 0.03),
        "seed": config.get("bridge_seed", -1),
        "use_end_frame": config.get("bridge_use_end_frame", True),
        "allow_bridge_fallback": config.get("bridge_allow_fallback", True),
        "image_duration": config.get("bridge_image_duration", 2.5),
    }
    merged.update(settings)

    job = job_manager.submit(
        JOB_BRIDGE, _bridges_worker, items, merged,
        label=f"{len(items)} clips, {len(items)-1} bridges",
    )
    return {"job_id": job.id}
