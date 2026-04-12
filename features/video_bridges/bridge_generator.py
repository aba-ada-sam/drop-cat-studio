"""WanGP bridge generation + OpenCV fallback + compilation.

Generates transition videos between clips and compiles the final output.
Ported from DropCatGo-Video-BRIDGES/bridge_generator.py.
"""
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from core import config as cfg
from core.ffmpeg_utils import parse_resolution, probe_duration

log = logging.getLogger(__name__)

FLUIDITY_PREFIX = (
    "Smooth continuous fluid motion, no flickering, no flashing, "
    "gradual seamless transformation with natural motion blur. "
)

MODELS = {
    "Wan2.1-I2V-14B-480P":    {"res": (854, 480),  "fps": 16, "max_sec": 16, "i2v": True},
    "Wan2.1-I2V-14B-720P":    {"res": (1280, 720), "fps": 16, "max_sec": 12, "i2v": True},
    "LTX-2 Dev19B Distilled": {"res": (1032, 580), "fps": 25, "max_sec": 20, "i2v": True},
    "Wan2.1-T2V-14B":         {"res": (854, 480),  "fps": 16, "max_sec": 16, "i2v": False},
    "Wan2.1-T2V-1.3B":        {"res": (854, 480),  "fps": 16, "max_sec": 12, "i2v": False},
}


def generate_bridge(
    frame_a_path: str,
    frame_b_path: str,
    prompt: str,
    out_path: str,
    duration: float = 10.0,
    model_name: str = "LTX-2 Dev19B Distilled",
    resolution: str = "480p",
    steps: int = 20,
    guidance: float = 10.0,
    seed: int = -1,
    use_end_frame: bool = True,
    allow_fallback: bool = True,
    stop_check=None,
    log_fn=None,
) -> str | None:
    """Generate a bridge video between two frames. Returns output path or None."""
    from features.fun_videos.video_generator import generate_video

    full_prompt = FLUIDITY_PREFIX + prompt

    result = generate_video(
        image_path=frame_a_path,
        prompt=full_prompt,
        out_path=out_path,
        duration=duration,
        model_name=model_name,
        resolution=resolution,
        steps=steps,
        guidance=guidance,
        seed=seed,
        end_image_path=frame_b_path if use_end_frame else None,
        stop_check=stop_check,
        log_fn=log_fn,
    )

    if result and os.path.isfile(result):
        return result

    if allow_fallback:
        if log_fn:
            log_fn("[warning] WanGP failed — using OpenCV morph fallback")
        return _generate_morph_fallback(
            frame_a_path, frame_b_path, prompt, out_path,
            duration=min(duration, 3.0), resolution=resolution, log_fn=log_fn,
        )

    return None


def _generate_morph_fallback(
    frame_a: str, frame_b: str, prompt: str, out_path: str,
    duration: float = 3.0, resolution: str = "480p", log_fn=None,
) -> str | None:
    """Generate a simple cross-fade morph using OpenCV."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        if log_fn:
            log_fn("[error] OpenCV not installed — cannot generate fallback")
        return None

    res_w, res_h = parse_resolution(resolution)
    fps = 24
    total_frames = max(12, int(duration * fps))

    img_a = cv2.imread(frame_a)
    img_b = cv2.imread(frame_b)
    if img_a is None or img_b is None:
        return None

    img_a = cv2.resize(img_a, (res_w, res_h))
    img_b = cv2.resize(img_b, (res_w, res_h))

    with tempfile.TemporaryDirectory(prefix="morph_") as tmpdir:
        for i in range(total_frames):
            t = i / max(1, total_frames - 1)
            ease = t * t * (3.0 - 2.0 * t)  # smoothstep
            frame = cv2.addWeighted(img_a, 1.0 - ease, img_b, ease, 0)
            cv2.imwrite(os.path.join(tmpdir, f"f{i:05d}.jpg"), frame)

        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(fps),
            "-i", os.path.join(tmpdir, "f%05d.jpg"),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            out_path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and os.path.isfile(out_path):
            return out_path

    return None


def compile_with_bridges(
    segment_paths: list[str],
    bridge_paths: list[str | None],
    out_path: str,
    resolution: str = "480p",
    segment_kinds: list[str] | None = None,
    image_duration: float = 2.5,
    log_fn=None,
) -> str | None:
    """Compile original clips interleaved with bridge videos."""
    res_w, res_h = parse_resolution(resolution)
    fps = 24

    with tempfile.TemporaryDirectory(prefix="compile_") as tmpdir:
        normalized = []

        for i, seg_path in enumerate(segment_paths):
            kind = (segment_kinds or ["video"] * len(segment_paths))[i]
            norm_path = os.path.join(tmpdir, f"seg_{i:03d}.mp4")

            if kind == "image":
                # Convert image to short video clip
                cmd = [
                    "ffmpeg", "-y", "-loop", "1",
                    "-t", str(image_duration),
                    "-i", seg_path,
                    "-vf", f"scale={res_w}:{res_h}:force_original_aspect_ratio=decrease,"
                           f"pad={res_w}:{res_h}:(ow-iw)/2:(oh-ih)/2:black,"
                           f"fps={fps},format=yuv420p",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    norm_path,
                ]
            else:
                cmd = [
                    "ffmpeg", "-y", "-i", seg_path,
                    "-vf", f"scale={res_w}:{res_h}:force_original_aspect_ratio=decrease,"
                           f"pad={res_w}:{res_h}:(ow-iw)/2:(oh-ih)/2:black,"
                           f"fps={fps},format=yuv420p",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
                    norm_path,
                ]

            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                if log_fn:
                    log_fn(f"[error] Failed to normalize segment {i}: {r.stderr[-200:]}")
                continue
            normalized.append(norm_path)

            # Add bridge after this segment (if not last)
            if i < len(bridge_paths):
                bridge = bridge_paths[i]
                if bridge and os.path.isfile(bridge):
                    bridge_norm = os.path.join(tmpdir, f"bridge_{i:03d}.mp4")
                    cmd = [
                        "ffmpeg", "-y", "-i", bridge,
                        "-vf", f"scale={res_w}:{res_h}:force_original_aspect_ratio=decrease,"
                               f"pad={res_w}:{res_h}:(ow-iw)/2:(oh-ih)/2:black,"
                               f"fps={fps},format=yuv420p",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
                        bridge_norm,
                    ]
                    r = subprocess.run(cmd, capture_output=True, text=True)
                    if r.returncode == 0:
                        normalized.append(bridge_norm)

        if not normalized:
            return None

        # Concat all normalized clips
        concat_txt = os.path.join(tmpdir, "concat.txt")
        with open(concat_txt, "w", encoding="utf-8") as f:
            for p in normalized:
                f.write(f"file '{p}'\n")

        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_txt,
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            out_path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and os.path.isfile(out_path):
            return out_path

        if log_fn:
            log_fn(f"[error] Final compilation failed: {r.stderr[-300:]}")
        return None
