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

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"

# Official release weights -- RealESRGANer auto-downloads these on first use
# when no local copy exists in MODELS_DIR.
_ESRGAN_URLS = {
    2: "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth",
    4: "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth",
}

# Dedicated CUDA venv (see tools/INSTALL_REALESRGAN.bat). The AI pass runs as
# a subprocess in this venv so the app's Python never needs the torch stack.
_VENV_PY = Path(__file__).resolve().parent.parent / "venv-upscale" / "Scripts" / "python.exe"
_AI_WORKER = Path(__file__).resolve().parent.parent / "tools" / "ai_upscale_frames.py"


# Temp dirs get this prefix so a startup sweep can reclaim them after a hard
# kill (a 4-min 1080p AI job stages tens of GB of frames in %TEMP%).
_TMP_PREFIX = "dcs-upscale-"


def _nearest_even(n: int) -> int:
    return n if n % 2 == 0 else n + 1


def cleanup_orphan_temp() -> int:
    """Delete leftover dcs-upscale-* temp dirs from killed jobs.

    Safe at app startup: single-instance means no upscale worker can be
    alive before the server is. Returns bytes freed (best effort).
    """
    import shutil

    freed = 0
    for d in Path(tempfile.gettempdir()).glob(_TMP_PREFIX + "*"):
        if not d.is_dir():
            continue
        try:
            freed += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        except OSError:
            pass
        shutil.rmtree(d, ignore_errors=True)
        log.info("[upscale] reclaimed orphan temp dir %s", d.name)
    return freed


def ai_available() -> bool:
    """True if AI upscaling can run -- venv-upscale exists or packages import."""
    if _VENV_PY.exists() and _AI_WORKER.exists():
        return True
    try:
        from basicsr.archs.rrdbnet_arch import RRDBNet  # noqa: F401
        from realesrgan import RealESRGANer  # noqa: F401
        return True
    except ImportError:
        return False


def _run_venv_worker(frames_dir: Path, up_dir: Path, scale: float, progress_cb) -> bool:
    """Run the Real-ESRGAN frame worker in venv-upscale. Returns success."""
    cmd = [str(_VENV_PY), str(_AI_WORKER), str(frames_dir), str(up_dir), str(scale)]
    tail: list[str] = []
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("PROGRESS "):
                try:
                    done, total = line.split()[1:3]
                    if progress_cb:
                        progress_cb(int(done) / int(total),
                                    f"AI upscaling frame {done}/{total}")
                except (ValueError, IndexError):
                    pass
            elif line:
                tail.append(line)
                if len(tail) > 20:
                    tail.pop(0)
        proc.wait(timeout=4 * 3600)
        if proc.returncode != 0:
            log.warning("[upscale] AI worker exited %s: %s",
                        proc.returncode, " | ".join(tail[-5:]))
            return False
        return True
    except Exception as e:
        log.warning("[upscale] AI worker error: %s", e)
        return False


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
    crf: int = 14,
    preset: str = "fast",
) -> tuple[str | None, str | None]:
    """Upscale video using ffmpeg lanczos. Returns (output_path, error).

    scale=1.0 skips the scale filter -- pure re-encode at the given CRF
    (the "optimize only" path for shrinking oversized files).
    """
    try:
        w, h, _ = _probe_video(input_path)
        new_w = _nearest_even(int(w * scale))
        new_h = _nearest_even(int(h * scale))

        from core.ffmpeg_utils import video_encode_args
        cmd = ["ffmpeg", "-y", "-i", input_path]
        if scale != 1.0:
            cmd += ["-vf", f"scale={new_w}:{new_h}:flags=lanczos"]
        cmd += video_encode_args(crf=int(crf))
        cmd += ["-c:a", "copy", output_path]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            return None, f"ffmpeg upscale failed: {r.stderr[-400:]}"
        if Path(output_path).exists() and Path(output_path).stat().st_size > 0:
            log.info("Upscaled %s -> %dx%d lanczos crf=%d", Path(input_path).name, new_w, new_h, crf)
            return output_path, None
        return None, "ffmpeg upscale produced empty output"
    except Exception as e:
        return None, f"upscale_ffmpeg error: {e}"


def upscale_ai(
    input_path: str,
    output_path: str,
    scale: float = 2.0,
    crf: int = 14,
    progress_cb=None,
) -> tuple[str | None, str | None]:
    """Upscale video with Real-ESRGAN (frame-by-frame). Falls back to ffmpeg.

    progress_cb, if given, is called as progress_cb(frac_0_to_1, message).

    Prefers the dedicated venv-upscale subprocess; falls back to in-process
    Real-ESRGAN if importable, else to ffmpeg lanczos.
    """
    use_venv = _VENV_PY.exists() and _AI_WORKER.exists()
    if not use_venv:
        try:
            from basicsr.archs.rrdbnet_arch import RRDBNet  # noqa: F401
            from realesrgan import RealESRGANer  # noqa: F401
        except ImportError:
            log.info("Real-ESRGAN not installed -- using ffmpeg lanczos upscale")
            return upscale_ffmpeg(input_path, output_path, scale, crf=crf)

    try:
        w, h, fps_str = _probe_video(input_path)

        with tempfile.TemporaryDirectory(prefix=_TMP_PREFIX) as tmp:
            frames_dir = Path(tmp) / "frames"
            up_dir = Path(tmp) / "up"
            frames_dir.mkdir(); up_dir.mkdir()

            # Extract frames
            if progress_cb:
                progress_cb(0.0, "Extracting frames...")
            ex = subprocess.run(
                ["ffmpeg", "-y", "-i", input_path, str(frames_dir / "f%05d.png")],
                capture_output=True, timeout=1800,
            )
            if ex.returncode != 0:
                log.warning("[upscale] Frame extract failed -- using ffmpeg")
                return upscale_ffmpeg(input_path, output_path, scale, crf=crf)

            frames = sorted(frames_dir.glob("*.png"))
            if not frames:
                return upscale_ffmpeg(input_path, output_path, scale, crf=crf)

            if use_venv:
                if not _run_venv_worker(frames_dir, up_dir, scale, progress_cb):
                    log.warning("[upscale] venv AI worker failed -- falling back to ffmpeg")
                    return upscale_ffmpeg(input_path, output_path, scale, crf=crf)
            else:
                import numpy as np
                import torch as _torch
                from basicsr.archs.rrdbnet_arch import RRDBNet
                from PIL import Image as _Img
                from realesrgan import RealESRGANer

                # Load model (x2 or x4 depending on target scale).
                # Prefer a local copy in MODELS_DIR; otherwise hand RealESRGANer
                # the release URL so it auto-downloads the weights on first use.
                model_scale = 4 if scale > 2.5 else 2
                local_weights = MODELS_DIR / f"RealESRGAN_x{model_scale}plus.pth"
                model_path = str(local_weights) if local_weights.exists() else _ESRGAN_URLS[model_scale]
                net = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64,
                              num_block=23, num_grow_ch=32, scale=model_scale)
                upsampler = RealESRGANer(
                    scale=model_scale,
                    model_path=model_path,
                    model=net, tile=512, tile_pad=10, pre_pad=0,
                    half=_torch.cuda.is_available(),  # fp16 convs unsupported on CPU
                )

                total = len(frames)
                for idx, frame in enumerate(frames):
                    img = np.array(_Img.open(frame).convert("RGB"))
                    out, _ = upsampler.enhance(img, outscale=scale)
                    _Img.fromarray(out).save(str(up_dir / frame.name))
                    if progress_cb and (idx % 10 == 0 or idx == total - 1):
                        progress_cb((idx + 1) / total, f"AI upscaling frame {idx + 1}/{total}")

            # Re-encode preserving audio
            if progress_cb:
                progress_cb(0.98, "Encoding output...")
            from core.ffmpeg_utils import video_encode_args
            r = subprocess.run(
                ["ffmpeg", "-y",
                 "-framerate", fps_str,
                 "-i", str(up_dir / "f%05d.png"),
                 "-i", input_path,
                 "-map", "0:v", "-map", "1:a?",
                 "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                 *video_encode_args(crf=int(crf)),
                 "-c:a", "copy", "-shortest",
                 output_path],
                capture_output=True, timeout=3600,
            )
            if r.returncode == 0 and Path(output_path).exists() and Path(output_path).stat().st_size > 0:
                log.info("AI upscaled %s x%.1f Real-ESRGAN", Path(input_path).name, scale)
                return output_path, None

            log.warning("[upscale] Real-ESRGAN re-encode failed -- falling back to ffmpeg")
    except Exception as e:
        log.warning("[upscale] Real-ESRGAN failed: %s -- falling back to ffmpeg", e)

    return upscale_ffmpeg(input_path, output_path, scale, crf=crf)


def upscale_video(
    input_path: str,
    output_path: str,
    scale: float = 2.0,
    method: str = "ffmpeg",
    crf: int = 14,
    preset: str = "fast",
    progress_cb=None,
) -> tuple[str | None, str | None]:
    """Upscale a video file. Returns (output_path, error_or_None).

    method: 'ffmpeg' (lanczos, default) or 'ai' (Real-ESRGAN with ffmpeg fallback).
    scale: output multiplier, e.g. 2.0 doubles both dimensions. 1.0 = re-encode
           only (optimize file size without resizing); AI is skipped at 1.0.
    """
    if method == "ai" and scale > 1.0:
        return upscale_ai(input_path, output_path, scale, crf=crf, progress_cb=progress_cb)
    return upscale_ffmpeg(input_path, output_path, scale, crf=crf, preset=preset)
