"""Image-to-Video API routes — /api/i2v/*

Ken Burns slideshow generator. Upload images, configure motion/duration,
generate combined or separate videos.
"""
import logging
import os
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile

from core import config as cfg
from core.ffmpeg_utils import ffmpeg_available
from core.job_manager import JOB_I2V
from features.image2video.generator import (
    IMAGE_EXTS,
    generate_video,
    read_image_size,
    resolve_target_size,
    sanitize_stem,
)

log = logging.getLogger(__name__)
router = APIRouter()

UPLOADS_DIR = Path(__file__).resolve().parent.parent.parent / "uploads"
OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"


def _image_payload(path: Path, name: str | None = None, size: int | None = None) -> dict:
    w, h = read_image_size(path)
    return {
        "path": str(path),
        "name": name or path.name,
        "size": size if size is not None else path.stat().st_size,
        "width": w,
        "height": h,
    }


def _normalize_spec(item) -> dict:
    if isinstance(item, str):
        return {"path": item, "name": Path(item).name, "motion": "random"}
    if not isinstance(item, dict):
        raise HTTPException(400, "Invalid image entry")
    path = str(item.get("path", "")).strip()
    if not path:
        raise HTTPException(400, "Image path missing")
    return {
        "path": path,
        "name": item.get("name") or Path(path).name,
        "size": item.get("size"),
        "width": item.get("width"),
        "height": item.get("height"),
        "motion": (item.get("motion") or "random").strip().lower(),
    }


# ── Upload ───────────────────────────────────────────────────────────────────

@router.post("/upload")
async def upload_images(files: list[UploadFile] = File(...)):
    saved = []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in IMAGE_EXTS:
            continue
        dest = UPLOADS_DIR / f"{uuid.uuid4()}{ext}"
        data = await f.read()
        dest.write_bytes(data)
        saved.append(_image_payload(dest, name=f.filename, size=len(data)))
    return {"images": saved}


# ── Folder scan ──────────────────────────────────────────────────────────────

@router.post("/scan_folder")
async def scan_folder(request: Request):
    body = await request.json()
    folder = body.get("folder", "").strip()
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "Invalid folder path")
    images = []
    for p in sorted(Path(folder).iterdir()):
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            images.append(_image_payload(p))
    return {"images": images, "folder": folder}


@router.get("/image")
async def serve_image(path: str = Query(...)):
    from fastapi.responses import FileResponse
    p = Path(path)
    if not p.exists() or not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
        raise HTTPException(404, "Image not found")
    return FileResponse(str(p))


# ── Generate ─────────────────────────────────────────────────────────────────

def _i2v_worker(job, image_specs, settings):
    """Background worker for image-to-video generation."""
    output_mode = (settings.get("output_mode") or "combined").strip().lower()
    aspect_mode = (settings.get("aspect_mode") or "auto").strip().lower()
    fit_mode = (settings.get("fit_mode") or "contain").strip().lower()
    res = settings.get("output_res", "1280x720")
    ts = time.strftime("%Y%m%d_%H%M%S")
    outputs = []

    if output_mode == "separate":
        for i, spec in enumerate(image_specs):
            if job.stop_event.is_set():
                break
            tw, th = resolve_target_size(res, aspect_mode, spec.get("width"), spec.get("height"))
            safe_name = sanitize_stem(spec.get("name") or Path(spec["path"]).name)
            out = OUTPUT_DIR / f"video_{ts}_{i+1:02d}_{safe_name}.mp4"
            generate_video(
                job, [spec],
                img_dur=float(settings.get("img_dur", 3.0)),
                fade_dur=float(settings.get("fade_dur", 0.5)),
                kb_zoom_pct=float(settings.get("ken_burns_zoom", 5)),
                target_w=tw, target_h=th,
                crf=int(settings.get("crf", 18)),
                fps=int(settings.get("fps", 30)),
                output_path=out, fit_mode=fit_mode,
            )
            outputs.append(out.name)
    else:
        ref = image_specs[0]
        tw, th = resolve_target_size(res, aspect_mode, ref.get("width"), ref.get("height"))
        out = OUTPUT_DIR / f"video_{ts}.mp4"
        generate_video(
            job, image_specs,
            img_dur=float(settings.get("img_dur", 3.0)),
            fade_dur=float(settings.get("fade_dur", 0.5)),
            kb_zoom_pct=float(settings.get("ken_burns_zoom", 5)),
            target_w=tw, target_h=th,
            crf=int(settings.get("crf", 18)),
            fps=int(settings.get("fps", 30)),
            output_path=out, fit_mode=fit_mode,
        )
        outputs.append(out.name)

    if not job.stop_event.is_set():
        job.output = outputs[0] if outputs else None
        job.meta["outputs"] = outputs
        job.message = f"Done! Created {len(outputs)} video(s)." if output_mode == "separate" else "Done!"
        from core.session import get_current as get_session
        for out_name in outputs:
            get_session().add_file(out_name, "video", "image2video", path=str(OUTPUT_DIR / out_name))


@router.post("/generate")
async def start_generate(request: Request):
    if not ffmpeg_available():
        raise HTTPException(
            503,
            "ffmpeg is not installed or not on PATH. "
            "Install ffmpeg and add its bin folder to your system PATH, then restart the app."
        )

    # Import here to access the global job_manager from app
    from app import get_job_manager; job_manager = get_job_manager()

    body = await request.json()
    images = body.get("images") or body.get("image_paths", [])
    settings = body.get("settings", {})

    image_specs = [_normalize_spec(item) for item in images]
    if not image_specs:
        raise HTTPException(400, "No images provided")

    # Fill in missing dimensions
    for spec in image_specs:
        path = Path(spec["path"])
        if not path.exists():
            raise HTTPException(400, f"Image not found: {spec['path']}")
        if not spec.get("width") or not spec.get("height"):
            spec["width"], spec["height"] = read_image_size(path)

    # Use config defaults for missing settings
    config = cfg.load()
    merged = {
        "img_dur": config.get("i2v_img_dur", 3.0),
        "fade_dur": config.get("i2v_fade_dur", 0.5),
        "ken_burns_zoom": config.get("i2v_ken_burns_zoom", 5),
        "output_res": config.get("i2v_output_res", "1280x720"),
        "aspect_mode": config.get("i2v_aspect_mode", "auto"),
        "fit_mode": config.get("i2v_fit_mode", "contain"),
        "output_mode": config.get("i2v_output_mode", "combined"),
        "crf": config.get("i2v_crf", 18),
        "fps": config.get("i2v_fps", 30),
    }
    merged.update(settings)

    job = job_manager.submit(
        JOB_I2V, _i2v_worker, image_specs, merged,
        label=f"{len(image_specs)} images",
    )
    return {"job_id": job.id}
