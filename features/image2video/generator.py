"""Ken Burns video generator — converts images into videos with motion effects.

Extracted from DropCat-Image-2-Video/web_server.py. Pure ffmpeg, no AI.
"""
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from core.ffmpeg_utils import find_ffmpeg, round_even

log = logging.getLogger(__name__)

FFMPEG = find_ffmpeg() or "ffmpeg"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}


def sanitize_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem).strip("._-")
    return stem[:40] or "image"


def read_image_size(path: Path) -> tuple[int | None, int | None]:
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            return int(img.size[0]), int(img.size[1])
    except Exception:
        return None, None


def resolve_motion_mode(mode: str, seed: str) -> str:
    normalized = (mode or "random").strip().lower()
    if normalized in {"zoom_in", "in", "zoomin"}:
        return "zoom_in"
    if normalized in {"zoom_out", "out", "zoomout"}:
        return "zoom_out"
    if normalized in {"still", "off", "none"}:
        return "still"
    digest = hashlib.md5(seed.encode("utf-8", errors="ignore")).digest()
    return "zoom_in" if digest[0] % 2 == 0 else "zoom_out"


def parse_resolution(value: str) -> tuple[int, int]:
    try:
        w, h = (int(x) for x in str(value).lower().split("x", 1))
        return round_even(w), round_even(h)
    except Exception:
        return 1280, 720


def resolve_target_size(base_res: str, aspect_mode: str,
                        ref_width: int | None, ref_height: int | None) -> tuple[int, int]:
    base_w, base_h = parse_resolution(base_res)
    mode = (aspect_mode or "auto").strip().lower()

    if mode == "fixed" or not ref_width or not ref_height:
        return base_w, base_h
    if mode == "auto":
        if ref_width == ref_height:
            side = round_even(min(base_w, base_h))
            return side, side
        if ref_width > ref_height:
            return max(base_w, base_h), min(base_w, base_h)
        return min(base_w, base_h), max(base_w, base_h)
    if mode == "source":
        long_side = max(base_w, base_h)
        if ref_width >= ref_height:
            width = long_side
            height = round_even(long_side * (ref_height / ref_width))
        else:
            height = long_side
            width = round_even(long_side * (ref_width / ref_height))
        return width, height
    return base_w, base_h


def build_kb_filter(kb_zoom: float, seg_dur: float, fps: int,
                    tw: int, th: int, motion_mode: str,
                    fit_mode: str) -> tuple[str, int]:
    """Return ffmpeg filter string and frame count for one Ken Burns segment."""
    total_frames = max(1, round(seg_dur * fps))

    if tw <= 1920 and th <= 1920:
        work_w, work_h = tw * 2, th * 2
    else:
        work_w, work_h = tw, th

    mode = (fit_mode or "contain").strip().lower()
    if mode == "cover":
        prep = (
            f"scale={work_w}:{work_h}:force_original_aspect_ratio=increase,"
            f"crop={work_w}:{work_h}"
        )
    else:
        prep = (
            f"scale={work_w}:{work_h}:force_original_aspect_ratio=decrease,"
            f"pad={work_w}:{work_h}:(ow-iw)/2:(oh-ih)/2:color=black"
        )

    if kb_zoom <= 0 or motion_mode == "still":
        return f"{prep},scale={tw}:{th}:flags=lanczos,fps={fps},format=yuv420p", total_frames

    start_zoom = 1.0
    end_zoom = 1.0 + kb_zoom
    if motion_mode == "zoom_out":
        start_zoom, end_zoom = end_zoom, start_zoom

    denom = max(1, total_frames - 1)
    zoom_expr = f"{start_zoom:.6f}+({end_zoom - start_zoom:.6f}*on/{denom})"
    return (
        f"{prep},"
        f"zoompan="
        f"z='{zoom_expr}':"
        f"x='max(0,min(iw-iw/zoom,(iw-iw/zoom)/2))':"
        f"y='max(0,min(ih-ih/zoom,(ih-ih/zoom)/2))':"
        f"d={total_frames}:s={tw}x{th}:fps={fps},"
        f"fps={fps},"
        f"format=yuv420p"
    ), total_frames


def generate_video(
    job,
    image_specs: list[dict],
    img_dur: float,
    fade_dur: float,
    kb_zoom_pct: float,
    target_w: int,
    target_h: int,
    crf: int,
    fps: int,
    output_path: Path,
    fit_mode: str,
):
    """Core Ken Burns video generator. Updates job.progress/message."""
    kb_zoom_pct = max(0.0, min(float(kb_zoom_pct), 30.0))  # clamp to 0-30%
    kb_zoom = kb_zoom_pct / 100.0
    fade_dur = min(fade_dur, max(0.0, img_dur - 0.1))
    n = len(image_specs)

    job.update(progress=5, message=f"Starting — {n} image(s)")

    with tempfile.TemporaryDirectory(prefix="dciv_") as tmpdir:
        seg_paths = []

        # Phase 1: create a video segment per image
        for i, spec in enumerate(image_specs):
            if job.stop_event.is_set():
                return

            img_path = spec["path"]
            motion = resolve_motion_mode(
                spec.get("motion", "random"),
                f"{img_path}|{i}|{job.id}",
            )

            pct = 5 + int(i / n * 60)
            job.update(progress=pct, message=f"Processing image {i+1}/{n} ({motion})")

            seg_dur = img_dur + (fade_dur if fade_dur > 0 and n > 1 else 0)
            vf, total_frames = build_kb_filter(
                kb_zoom, seg_dur, fps, target_w, target_h, motion, fit_mode,
            )
            seg_path = os.path.join(tmpdir, f"seg_{i:04d}.mp4")

            cmd = [
                FFMPEG, "-y",
                "-loop", "1", "-i", str(img_path),
                "-vf", vf,
                "-frames:v", str(total_frames),
                "-c:v", "libx264", "-crf", str(crf), "-preset", "fast",
                "-pix_fmt", "yuv420p",
                seg_path,
            ]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                snippet = r.stderr[-600:] if r.stderr else "(no stderr)"
                raise RuntimeError(f"ffmpeg failed on image {i+1}: {snippet}")
            seg_paths.append(seg_path)

        if job.stop_event.is_set():
            return

        # Phase 2: concatenate
        if len(seg_paths) == 1:
            job.update(progress=80, message="Finalising...")
            shutil.copy(seg_paths[0], str(output_path))

        elif fade_dur <= 0:
            job.update(progress=75, message="Concatenating clips...")
            concat_txt = os.path.join(tmpdir, "concat.txt")
            with open(concat_txt, "w", encoding="utf-8") as f:
                for sp in seg_paths:
                    f.write(f"file '{sp}'\n")
            cmd = [FFMPEG, "-y", "-f", "concat", "-safe", "0",
                   "-i", concat_txt, "-c", "copy", str(output_path)]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"Concat failed: {r.stderr[-600:]}")

        else:
            job.update(progress=75, message="Applying crossfade transitions...")
            inputs = []
            for sp in seg_paths:
                inputs += ["-i", sp]

            parts = []
            prev_lbl = None
            for i in range(n - 1):
                in0 = prev_lbl or f"[{i}:v]"
                in1 = f"[{i+1}:v]"
                is_last = (i == n - 2)
                out_lbl = "[vout]" if is_last else f"[xf{i+1}]"
                offset = (i + 1) * img_dur
                parts.append(
                    f"{in0}{in1}xfade=transition=fade:"
                    f"duration={fade_dur:.4f}:offset={offset:.4f}{out_lbl}"
                )
                prev_lbl = out_lbl

            cmd = (
                [FFMPEG, "-y"] + inputs + [
                    "-filter_complex", ";".join(parts),
                    "-map", "[vout]",
                    "-c:v", "libx264", "-crf", str(crf), "-preset", "fast",
                    "-pix_fmt", "yuv420p",
                    str(output_path),
                ]
            )
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"xfade failed: {r.stderr[-800:]}")

        job.update(progress=95, message="Complete!")
