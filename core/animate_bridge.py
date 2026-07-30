"""Shared "animate this image" bridge into the fun_videos pipeline.

Extracted from features/chat_studio/routes.py's /animate handler so a second
caller (features/image_studio) doesn't need its own copy of the per-model
step-floor dict -- there is already a third copy of this floor in
features/fun_videos/routes.py's make-it handler (handler-local, can't be
imported); this module is the single shared copy for chat_studio + image_studio.
"""
import os
from pathlib import Path

from fastapi import HTTPException

from core import config as cfg

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

_MIN_STEPS = {
    "LTX-2 Dev19B Distilled": 4,
    "LTX-2 Dev13B":            20,
    "LTX-2 Dev13B 360P":       20,
    "Wan2.1-I2V-14B-480P":     20,
    "Wan2.1-I2V-14B-720P":     20,
    "Wan2.1-T2V-14B":          20,
    "Wan2.1-T2V-1.3B":         15,
}


def _in_output_dir(p: str) -> bool:
    try:
        return Path(p).resolve().is_relative_to(OUTPUT_DIR.resolve())
    except (OSError, ValueError):
        return False


def _valid_output_file(p: str) -> bool:
    """Combined containment + existence check, one failure message either way
    -- checking os.path.isfile() before containment would let a caller learn
    whether an arbitrary out-of-bounds path exists on disk."""
    return bool(p) and _in_output_dir(p) and os.path.isfile(p)


def _safe_float(val, default: float) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_int(val, default: int) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


async def animate_image(body: dict, label_prefix: str = "Animate") -> dict:
    from app import get_job_manager
    from core.job_manager import JOB_FUN_VIDEO
    from features.fun_videos.pipeline import run_prep, run_pipeline
    from features.fun_videos.video_generator import MODELS as _VG_MODELS

    image_path = body.get("image_path")
    if not isinstance(image_path, str) or not _valid_output_file(image_path):
        raise HTTPException(400, "image_path must be a real file inside the output directory")
    video_prompt = body.get("video_prompt")
    video_prompt = video_prompt.strip() if isinstance(video_prompt, str) else ""
    if not video_prompt:
        raise HTTPException(400, "video_prompt required")

    from core.minor_safety import nsfw_render_blocked
    if await nsfw_render_blocked(video_prompt):
        raise HTTPException(403, "Blocked by safety check -- this request could not be verified as depicting adults only.")

    config = cfg.load()
    requested_model = config.get("wan_model") or "LTX-2 Dev19B Distilled"

    # Reject T2V models upfront -- WanGP would otherwise silently drop the
    # start image and the job fails with an opaque error (same guard as
    # /api/fun/make-it).
    model_def = _VG_MODELS.get(requested_model)
    if model_def is not None and not model_def.get("i2v", True):
        raise HTTPException(
            400,
            f"{requested_model} is text-to-video and cannot accept a start image. "
            f"Pick an I2V model in Settings.",
        )

    duration = _safe_float(body.get("duration"), config.get("fun_video_duration", 6.0))
    duration = max(1.0, min(20.0, duration))

    video_steps = max(_safe_int(config.get("fun_video_steps"), 30), _MIN_STEPS.get(requested_model, 20))

    settings = {
        "video_prompt":   video_prompt,
        "skip_audio":     True,
        "model_name":     requested_model,
        "resolution":     config.get("resolution", "580p"),
        "video_duration": duration,
        "video_steps":    video_steps,
        "video_guidance": config.get("fun_video_guidance", 7.5),
        "video_seed":     config.get("fun_video_seed", -1),
        "use_wildcards":  False,
        "instrumental":   True,
        "upscale":        False,
    }

    label = f"{label_prefix}: {Path(image_path).stem[:20]}"
    job_manager = get_job_manager()
    try:
        job = job_manager.submit_with_prep(
            JOB_FUN_VIDEO, run_prep, run_pipeline, image_path, settings, label=label,
        )
    except RuntimeError as e:
        raise HTTPException(429, str(e))

    job.meta.update({
        "feature":      "fun_video",
        "source_image": image_path,
        "prompt":       settings.get("video_prompt", "")[:120],
        "model":        settings.get("model_name", ""),
        "settings": {
            "prompt":       settings.get("video_prompt", "")[:240],
            "steps":        settings.get("video_steps"),
            "guidance":     settings.get("video_guidance"),
            "duration_sec": settings.get("video_duration"),
            "source_image": image_path,
            "model":        settings.get("model_name", ""),
            "seed":         settings.get("video_seed"),
        },
    })
    return {"job_id": job.id}
