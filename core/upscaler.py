"""Video upscaling: ffmpeg lanczos (default) or AI Real-ESRGAN (optional).

upscale_video() is the single entry point.  method='ffmpeg' uses scale=lanczos
via ffmpeg (zero extra dependencies).  method='ai' attempts Real-ESRGAN
frame-by-frame and falls back to ffmpeg if the package is not installed.
"""
import logging
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def _nearest_even(n: int) -> int:
    return n if n % 2 == 0 else n + 1


def _probe_video(path: str) -> tuple[int, int, str]:
    """Return (width, height, fps_str) from ffprobe. Raises on failure."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate",
         "-of", "csv=p=0", path],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"ffprobe failed: {r.stderr[:200]}")
    parts = r.stdout.strip().split(",")
    return int(parts[0]), int(parts[1]), parts[2].strip() if len(parts) > 2 else "25"


def upscale_ffmpeg(
    input_path: str,
    output_path: str,
    scale: float = 2.0,
) -> tuple[str | None, str | None]:
    """Upscale video using ffmpeg lanczos. Returns (output_path, error)."""
    try:
        w, h, _ = _probe_video(input_path)
        new_w = _nearest_even(int(w * scale))
        new_h = _nearest_even(int(h * scale))

        r = subprocess.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-vf", f"scale={new_w}:{new_h}:flags=lanczos",
             "-c:v", "libx264", "-crf", "18", "-preset", "fast",
             "-c:a", "copy",
             output_path],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            return None, f"ffmpeg upscale failed: {r.stderr[-400:]}"
        if Path(output_path).exists() and Path(output_path).stat().st_size > 0:
            log.info("Upscaled %s -> %dx%d lanczos", Path(input_path).name, new_w, new_h)
            return output_path, None
        return None, "ffmpeg upscale produced empty output"
    except Exception as e:
        return None, f"upscale_ffmpeg error: {e}"


def upscale_ai(
    input_path: str,
    output_path: str,
    scale: float = 2.0,
) -> tuple[str | None, str | None]:
    """Upscale video with Real-ESRGAN (frame-by-frame). Falls back to ffmpeg."""
    try:
        from basicsr.archs.rrdbnet_arch import RRDBNet  # noqa: F401
        from realesrgan import RealESRGANer
    except ImportError:
        log.info("Real-ESRGAN not installed -- using ffmpeg lanczos upscale")
        return upscale_ffmpeg(input_path, output_path, scale)

    try:
        import numpy as np
        from PIL import Image as _Img

        w, h, fps_str = _probe_video(input_path)

        with tempfile.TemporaryDirectory() as tmp:
            frames_dir = Path(tmp) / "frames"
            up_dir = Path(tmp) / "up"
            frames_dir.mkdir(); up_dir.mkdir()

            # Extract frames
            ex = subprocess.run(
                ["ffmpeg", "-y", "-i", input_path, str(frames_dir / "f%05d.png")],
                capture_output=True, timeout=120,
            )
            if ex.returncode != 0:
                log.warning("[upscale] Frame extract failed -- using ffmpeg")
                return upscale_ffmpeg(input_path, output_path, scale)

            frames = sorted(frames_dir.glob("*.png"))
            if not frames:
                return upscale_ffmpeg(input_path, output_path, scale)

            # Load model (x2 or x4 depending on target scale)
            model_scale = 4 if scale > 2.5 else 2
            net = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64,
                          num_block=23, num_grow_ch=32, scale=model_scale)
            upsampler = RealESRGANer(
                scale=model_scale,
                model_path=f"models/RealESRGAN_x{model_scale}plus.pth",
                model=net, tile=512, tile_pad=10, pre_pad=0, half=True,
            )

            for frame in frames:
                img = np.array(_Img.open(frame).convert("RGB"))
                out, _ = upsampler.enhance(img, outscale=scale)
                _Img.fromarray(out).save(str(up_dir / frame.name))

            # Re-encode preserving audio
            r = subprocess.run(
                ["ffmpeg", "-y",
                 "-framerate", fps_str,
                 "-i", str(up_dir / "f%05d.png"),
                 "-i", input_path,
                 "-map", "0:v", "-map", "1:a?",
                 "-c:v", "libx264", "-crf", "18", "-preset", "fast",
                 "-c:a", "copy", "-shortest",
                 output_path],
                capture_output=True, timeout=300,
            )
            if r.returncode == 0 and Path(output_path).exists() and Path(output_path).stat().st_size > 0:
                log.info("AI upscaled %s x%.1f Real-ESRGAN", Path(input_path).name, scale)
                return output_path, None

            log.warning("[upscale] Real-ESRGAN re-encode failed -- falling back to ffmpeg")
    except Exception as e:
        log.warning("[upscale] Real-ESRGAN failed: %s -- falling back to ffmpeg", e)

    return upscale_ffmpeg(input_path, output_path, scale)


def upscale_video(
    input_path: str,
    output_path: str,
    scale: float = 2.0,
    method: str = "ffmpeg",
) -> tuple[str | None, str | None]:
    """Upscale a video file. Returns (output_path, error_or_None).

    method: 'ffmpeg' (lanczos, default) or 'ai' (Real-ESRGAN with ffmpeg fallback).
    scale: output multiplier, e.g. 2.0 doubles both dimensions.
    """
    if method == "ai":
        return upscale_ai(input_path, output_path, scale)
    return upscale_ffmpeg(input_path, output_path, scale)
