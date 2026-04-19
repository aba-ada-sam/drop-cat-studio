"""FFmpeg/FFprobe utility functions shared across all features.

Consolidates ffmpeg helpers from Image2Video, Video Reverser, Fun-Videos,
and BRIDGES into a single module.
"""
import json
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def find_ffmpeg() -> str | None:
    """Return path to ffmpeg binary, or None if not found."""
    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")


def find_ffprobe() -> str | None:
    """Return path to ffprobe binary, or None if not found."""
    return shutil.which("ffprobe") or shutil.which("ffprobe.exe")


def ffmpeg_available() -> bool:
    """Check if ffmpeg is accessible on PATH."""
    return find_ffmpeg() is not None


def probe_file(path: str | Path) -> dict:
    """Extract video metadata via ffprobe.

    Returns dict with keys: duration, width, height, has_audio, fps.
    Falls back to sensible defaults if probe fails.
    """
    result = {
        "duration": 0.0,
        "width": None,
        "height": None,
        "has_audio": False,
        "fps": 30.0,
    }
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-print_format", "json",
                "-show_format", "-show_streams",
                str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return result

        data = json.loads(r.stdout)

        # Duration from format
        fmt = data.get("format", {})
        result["duration"] = float(fmt.get("duration", 0))

        # Find video and audio streams
        for s in data.get("streams", []):
            codec_type = s.get("codec_type", "")
            if codec_type == "video" and result["width"] is None:
                result["width"] = int(s.get("width", 0)) or None
                result["height"] = int(s.get("height", 0)) or None
                # Parse FPS from r_frame_rate (e.g. "30000/1001")
                rfr = s.get("r_frame_rate", "")
                if "/" in rfr:
                    parts = rfr.split("/")
                    try:
                        num, den = float(parts[0]), float(parts[1])
                        if den > 0:
                            result["fps"] = round(num / den, 2)
                    except (ValueError, ZeroDivisionError):
                        pass
                elif rfr:
                    try:
                        result["fps"] = float(rfr)
                    except ValueError:
                        pass
            elif codec_type == "audio":
                result["has_audio"] = True

    except Exception as e:
        log.debug("probe_file(%s) failed: %s", path, e)

    return result


def probe_duration(path: str | Path) -> float:
    """Get duration in seconds via ffprobe. Returns 0.0 on failure."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except Exception:
        pass
    return 0.0


def probe_has_audio(path: str | Path) -> bool:
    """Check if a media file has an audio stream."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a",
                "-show_entries", "stream=codec_type",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        return bool(r.stdout.strip())
    except Exception:
        return False


def extract_frame_b64(
    video_path: str | Path,
    position: float = 0.5,
    max_dim: int = 1024,
) -> str | None:
    """Extract a single frame as base64-encoded JPEG.

    Args:
        video_path: Path to video or image file.
        position: Fraction of duration (0.0-1.0) to extract from.
        max_dim: Max width or height (for AI vision API limits).

    Returns:
        Base64 string or None on failure.
    """
    import base64

    path = Path(video_path)
    # For images, just read and encode
    if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
        try:
            from PIL import Image
            img = Image.open(path)
            img.thumbnail((max_dim, max_dim))
            import io
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=85)
            return base64.b64encode(buf.getvalue()).decode()
        except Exception:
            return None

    # For video, extract frame with ffmpeg
    dur = probe_duration(video_path)
    seek = dur * position if dur > 0 else 0

    try:
        r = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", f"{seek:.3f}",
                "-i", str(video_path),
                "-frames:v", "1",
                "-vf", f"scale='min({max_dim},iw)':'min({max_dim},ih)':force_original_aspect_ratio=decrease",
                "-f", "image2", "-c:v", "mjpeg",
                "-q:v", "4",
                "pipe:1",
            ],
            capture_output=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout:
            return base64.b64encode(r.stdout).decode()
    except Exception:
        pass
    return None


def run_ffmpeg(
    cmd: list[str],
    timeout: int = 3600,
    active_procs: set | None = None,
    procs_lock=None,
) -> subprocess.CompletedProcess:
    """Run an ffmpeg command with optional process tracking for stop-button kills."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if active_procs is not None and procs_lock is not None:
        with procs_lock:
            active_procs.add(proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        raise
    finally:
        if active_procs is not None and procs_lock is not None:
            with procs_lock:
                active_procs.discard(proc)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def round_even(value: float) -> int:
    """Round to nearest even integer (required for video dimensions)."""
    n = int(round(value))
    return n if n % 2 == 0 else n + 1


def parse_resolution(value: str) -> tuple[int, int]:
    """Parse resolution string like '1280x720' or '480p' into (width, height).

    Named presets: 480p, 580p, 720p, 1080p.
    """
    presets = {
        "480p":  (854, 480),
        "580p":  (1032, 580),
        "720p":  (1280, 720),
        "810p":  (1440, 810),
        "1080p": (1920, 1080),
        "1440p": (2560, 1440),
        "2160p": (3840, 2160),
    }
    value = value.strip().lower()
    if value in presets:
        return presets[value]
    if "x" in value:
        parts = value.split("x")
        try:
            return int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            pass
    return 1280, 720  # fallback
