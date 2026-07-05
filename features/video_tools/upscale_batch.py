"""Batch upscale/optimize worker for the Video Tools tab.

Wraps core.upscaler.upscale_video with job progress, inbox copy, and
session registration -- same conventions as reverser.process_batch.
"""
import logging
import os

from core.upscaler import ai_available, upscale_video

log = logging.getLogger(__name__)


def process_upscale_batch(job, file_list: list[str], settings: dict):
    """Upscale/optimize a batch of videos. Updates job.progress/message.

    Settings keys:
        scale   -- float multiplier; 1.0 = optimize only (re-encode, no resize)
        method  -- 'ffmpeg' (lanczos) or 'ai' (Real-ESRGAN, falls back to lanczos)
        crf     -- x264 quality, 0-51, lower = better
        out_dir -- output directory ('' = alongside source)
    """
    total = len(file_list)
    scale = float(settings.get("scale", 2.0))
    method = settings.get("method", "ffmpeg")
    crf = int(settings.get("crf", 18))
    out_dir = (settings.get("out_dir") or "").strip()

    ai_missing = method == "ai" and not ai_available()
    if ai_missing:
        log.warning("AI upscale requested but Real-ESRGAN not installed -- using Lanczos")

    suffix = "optimized" if scale <= 1.0 else "upscaled"
    results = []
    errors = []

    for i, src in enumerate(file_list):
        if job.stop_event.is_set():
            break

        basename = os.path.splitext(os.path.basename(src))[0]
        dest_dir = out_dir or os.path.dirname(src)
        os.makedirs(dest_dir, exist_ok=True)
        dst = os.path.join(dest_dir, f"{basename}_{suffix}.mp4")
        if os.path.normpath(dst) == os.path.normpath(src):
            dst = os.path.join(dest_dir, f"{basename}_{suffix}_2.mp4")

        base_pct = i / total * 100
        span = 100 / total * 0.95
        job.update(progress=int(base_pct),
                   message=f"{i + 1}/{total}: {os.path.basename(src)}")

        def _cb(frac, msg, _base=base_pct, _span=span, _n=i + 1):
            job.update(progress=int(_base + frac * _span), message=f"{_n}/{total}: {msg}")

        out_path, err = upscale_video(src, dst, scale=scale, method=method,
                                      crf=crf, progress_cb=_cb)
        if out_path:
            results.append(out_path)
            log.info("Done: %s", os.path.basename(out_path))
        else:
            errors.append(f"{os.path.basename(src)}: {err}")
            log.error("Failed %s: %s", os.path.basename(src), err)

    if not job.stop_event.is_set():
        job.output = results[0] if results else None
        from core.inbox import copy_to_inbox
        for _r in results:
            copy_to_inbox(_r)
        job.meta["outputs"] = results
        job.meta["total"] = total
        job.meta["processed"] = len(results)
        job.meta["errors"] = errors
        from core.session import get_current as get_session
        for r in results:
            get_session().add_file(os.path.basename(r), "video", "video_tools", path=r)

        method_note = ""
        if ai_missing:
            method_note = " (AI unavailable -- used Lanczos; run tools\\INSTALL_REALESRGAN.bat)"
        job.message = f"Processed {len(results)}/{total} files{method_note}"
        if errors and not results:
            raise RuntimeError("; ".join(errors[:3]))
