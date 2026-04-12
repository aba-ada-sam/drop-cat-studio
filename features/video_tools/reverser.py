"""Batch video transforms — reverse, mirror, flip, speed, upscale, sharpen.

Ported from Video Reverser/video_reverser.py (Tkinter → REST).
The core logic is the ffmpeg filter chain builder.
"""
import logging
import os
import subprocess
from pathlib import Path

from core.ffmpeg_utils import probe_file

log = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv", ".flv", ".m4v"}


def build_ffmpeg_cmd(src: str, dst: str, settings: dict) -> list[str]:
    """Build the ffmpeg command for one file.

    Settings keys:
        reverse_vid, reverse_aud, mirror, vflip, speed (0.25-4.0),
        keep_audio, mute_audio, volume (0-300),
        upscale (bool), upscale_mode (1.5x/2x/3x/4x/Custom),
        upscale_method (lanczos/bicubic/bilinear/spline),
        custom_w, custom_h,
        sharpen (bool), sharpen_str (0.5-3.0),
        out_format (mp4/mkv/mov/webm), crf (0-51)
    """
    info = probe_file(src)
    speed = float(settings.get("speed", 1.0))
    vfilters = []
    afilters = []

    # Video reverse
    if settings.get("reverse_vid", False):
        vfilters.append("reverse")

    # Speed (setpts for video)
    if speed != 1.0:
        pts = 1.0 / speed
        vfilters.append(f"setpts={pts:.4f}*PTS")

    # Mirror / V-Flip
    if settings.get("mirror", False):
        vfilters.append("hflip")
    if settings.get("vflip", False):
        vfilters.append("vflip")

    # Upscale
    if settings.get("upscale", False):
        method = settings.get("upscale_method", "lanczos")
        mode = settings.get("upscale_mode", "2x")
        if mode == "Custom":
            try:
                tw = int(settings.get("custom_w", -1))
            except (ValueError, TypeError):
                tw = -1
            try:
                th = int(settings.get("custom_h", -1))
            except (ValueError, TypeError):
                th = -1
            if tw > 0:
                tw = tw + (tw % 2)
            if th > 0:
                th = th + (th % 2)
            w_str = str(tw) if tw > 0 else "-2"
            h_str = str(th) if th > 0 else "-2"
        else:
            multiplier = float(mode.replace("x", ""))
            src_w = info.get("width") or 1920
            src_h = info.get("height") or 1080
            tw = int(src_w * multiplier)
            th = int(src_h * multiplier)
            tw = tw + (tw % 2)
            th = th + (th % 2)
            w_str = str(tw)
            h_str = str(th)
        vfilters.append(f"scale={w_str}:{h_str}:flags={method}")

    # Sharpen (unsharp mask)
    if settings.get("sharpen", False):
        strength = float(settings.get("sharpen_str", 1.0))
        vfilters.append(f"unsharp=5:5:{strength:.1f}:5:5:0.0")

    # Audio handling
    has_audio = (
        info.get("has_audio", False)
        and settings.get("keep_audio", True)
        and not settings.get("mute_audio", False)
    )

    if has_audio:
        if settings.get("reverse_aud", False):
            afilters.append("areverse")
        if speed != 1.0:
            remaining = speed
            while remaining > 2.0:
                afilters.append("atempo=2.0")
                remaining /= 2.0
            while remaining < 0.5:
                afilters.append("atempo=0.5")
                remaining /= 0.5
            if remaining != 1.0:
                afilters.append(f"atempo={remaining:.4f}")
        vol = int(settings.get("volume", 100))
        if vol != 100:
            afilters.append(f"volume={vol / 100.0:.2f}")

    # Build command
    fmt = settings.get("out_format", "mp4")
    crf = int(settings.get("crf", 18))

    cmd = ["ffmpeg", "-y", "-i", src]

    if vfilters:
        cmd += ["-vf", ",".join(vfilters)]
    if has_audio and afilters:
        cmd += ["-af", ",".join(afilters)]

    if fmt == "webm":
        cmd += ["-c:v", "libvpx-vp9", "-crf", str(crf), "-b:v", "0"]
        if has_audio:
            cmd += ["-c:a", "libopus", "-b:a", "192k"]
    else:
        cmd += ["-c:v", "libx264", "-crf", str(crf), "-preset", "medium"]
        if has_audio:
            cmd += ["-c:a", "aac", "-b:a", "256k"]

    if not has_audio:
        cmd += ["-an"]

    cmd.append(dst)
    return cmd


def process_batch(job, file_list: list[str], settings: dict):
    """Process a batch of video files. Updates job.progress/message."""
    total = len(file_list)
    fmt = settings.get("out_format", "mp4")
    out_dir = settings.get("out_dir", "").strip()
    results = []

    for i, src in enumerate(file_list):
        if job.stop_event.is_set():
            break

        basename = os.path.splitext(os.path.basename(src))[0]
        out_name = f"{basename}_processed.{fmt}"

        dest_dir = out_dir or os.path.dirname(src)
        os.makedirs(dest_dir, exist_ok=True)
        dst = os.path.join(dest_dir, out_name)

        # Avoid overwriting source
        if os.path.normpath(dst) == os.path.normpath(src):
            dst = os.path.join(dest_dir, f"{basename}_rev.{fmt}")

        pct = int(i / total * 90)
        job.update(progress=pct, message=f"Processing {i+1}/{total}: {os.path.basename(src)}")

        cmd = build_ffmpeg_cmd(src, dst, settings)
        log.info("cmd: %s...", " ".join(cmd[:8]))

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if result.returncode == 0:
                results.append(dst)
                log.info("Done: %s", os.path.basename(dst))
            else:
                stderr = result.stderr[-300:] if result.stderr else "unknown error"
                log.error("Failed %s: %s", os.path.basename(src), stderr)
        except subprocess.TimeoutExpired:
            log.error("Timeout on %s", os.path.basename(src))
        except Exception as e:
            log.error("Error on %s: %s", os.path.basename(src), e)

    if not job.stop_event.is_set():
        job.output = results[0] if results else None
        job.meta["outputs"] = results
        job.meta["total"] = total
        job.meta["processed"] = len(results)
        job.message = f"Processed {len(results)}/{total} files"
        from core.session import get_current as get_session
        for r in results:
            get_session().add_file(os.path.basename(r), "video", "video_tools", path=r)
