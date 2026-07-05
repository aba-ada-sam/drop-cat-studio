"""Frame interpolation -- fill in missing frames to smooth jerky footage.

Two modes:
  blend   -- ffmpeg minterpolate blend (fast, always available, decent)
  mci     -- ffmpeg minterpolate motion-compensated (slow, higher quality)
  rife    -- RIFE-NCNN-Vulkan if rife-ncnn-vulkan.exe exists in PATH or
             C:\\rife-ncnn-vulkan\\ (best quality, GPU-accelerated)

Typical use: footage that was time-stretched or squeezed in an NLE,
leaving segments with very few real frames per second.
"""
import collections
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from core.ffmpeg_utils import probe_file

log = logging.getLogger(__name__)

RIFE_SEARCH_PATHS = [
    r"C:\rife-ncnn-vulkan\rife-ncnn-vulkan.exe",
    r"C:\tools\rife-ncnn-vulkan\rife-ncnn-vulkan.exe",
]


def _describe_unreadable(path: str) -> str:
    """Ask ffprobe why a file won't open and return a short, plain-English reason."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        err = (r.stderr or "").strip()
    except Exception as e:  # noqa: BLE001 -- best-effort diagnostic
        err = str(e)
    low = err.lower()
    if "moov atom not found" in low:
        return ("the file is incomplete or corrupt (no 'moov' index) -- it was most "
                "likely never finished writing. Re-export it or pick an intact copy.")
    if "invalid data found" in low:
        return "the file is corrupt or not a valid video."
    if err:
        return err.splitlines()[-1]
    return "ffprobe found no readable video stream in it."


def _find_rife() -> str | None:
    exe = shutil.which("rife-ncnn-vulkan")
    if exe:
        return exe
    for p in RIFE_SEARCH_PATHS:
        if os.path.isfile(p):
            return p
    return None


def interpolate_video(
    job,
    src: str,
    dst: str,
    target_fps: float,
    mode: str = "blend",
) -> None:
    """Interpolate frames in src -> dst.

    job        -- Job object for progress + cancellation
    src        -- input video path
    dst        -- output video path (caller ensures parent dir exists)
    target_fps -- desired output frame rate (e.g. 60.0)
    mode       -- "blend", "mci", or "rife"
    """
    info = probe_file(src)
    if not info.get("width"):
        # probe_file swallows failures and returns defaults, so a missing width
        # means ffprobe could not actually read the file. Fail early with a reason
        # the user can act on instead of letting ffmpeg die on it later.
        raise RuntimeError(f"Can't read '{Path(src).name}': {_describe_unreadable(src)}")
    src_fps = float(info.get("fps", 24))

    if target_fps <= src_fps:
        target_fps = src_fps * 2

    job.update(progress=5, message=f"Interpolating {Path(src).name} {src_fps:.1f}->{target_fps:.0f}fps ({mode})")

    if mode == "rife":
        rife = _find_rife()
        if rife:
            _run_rife(job, rife, src, dst, src_fps, target_fps)
            return
        log.warning("RIFE not found, falling back to mci")
        mode = "mci"

    _run_ffmpeg_minterpolate(job, src, dst, target_fps, mode)


def _run_ffmpeg_minterpolate(job, src: str, dst: str, fps: float, mode: str) -> None:
    if mode == "mci":
        mi_filter = (
            f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:"
            f"me_mode=bidir:mb_size=16:vsbmc=1"
        )
    else:
        mi_filter = f"minterpolate=fps={fps}:mi_mode=blend"

    cmd = [
        "ffmpeg", "-y", "-i", src,
        "-vf", mi_filter,
        "-c:v", "libx264", "-crf", "17", "-preset", "fast",
        "-c:a", "copy",
        dst,
    ]
    log.info("[interpolate] ffmpeg cmd: %s", " ".join(cmd))
    job.update(progress=10, message=f"Running ffmpeg {mode} interpolation...")

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    duration_sec = None
    tail = collections.deque(maxlen=12)  # keep last lines to explain a failure
    for line in proc.stdout:
        if job.stop_event.is_set():
            proc.terminate()
            raise RuntimeError("Cancelled")
        line = line.strip()
        if line:
            tail.append(line)
        if "Duration:" in line and duration_sec is None:
            try:
                t = line.split("Duration:")[1].split(",")[0].strip()
                h, m, s = t.split(":")
                duration_sec = int(h)*3600 + int(m)*60 + float(s)
            except Exception:
                pass
        if "time=" in line and duration_sec:
            try:
                t = line.split("time=")[1].split()[0]
                h, m, s = t.split(":")
                elapsed = int(h)*3600 + int(m)*60 + float(s)
                pct = min(95, int(10 + 85 * elapsed / duration_sec))
                job.update(progress=pct)
            except Exception:
                pass

    proc.wait()
    if proc.returncode != 0:
        detail = " | ".join(t for t in tail) or "no output captured"
        raise RuntimeError(f"ffmpeg {mode} interpolation failed: {detail[-500:]}")
    job.update(progress=97, message="Interpolation complete")


def _run_rife(job, rife_exe: str, src: str, dst: str, src_fps: float, target_fps: float) -> None:
    multiplier = round(target_fps / src_fps)
    if multiplier < 2:
        multiplier = 2

    with tempfile.TemporaryDirectory() as tmp:
        frames_in = os.path.join(tmp, "in")
        frames_out = os.path.join(tmp, "out")
        os.makedirs(frames_in)
        os.makedirs(frames_out)

        job.update(progress=8, message="Extracting frames for RIFE...")
        subprocess.run([
            "ffmpeg", "-y", "-i", src, "-q:v", "2",
            os.path.join(frames_in, "frame%08d.png")
        ], check=True, capture_output=True)

        if job.stop_event.is_set():
            raise RuntimeError("Cancelled")

        job.update(progress=20, message=f"Running RIFE x{multiplier}...")
        subprocess.run([
            rife_exe,
            "-i", frames_in,
            "-o", frames_out,
            "-m", "rife-v4.6",
            "-n", str(multiplier),
        ], check=True, capture_output=True)

        if job.stop_event.is_set():
            raise RuntimeError("Cancelled")

        job.update(progress=80, message="Re-encoding with RIFE frames...")
        out_fps = src_fps * multiplier
        subprocess.run([
            "ffmpeg", "-y",
            "-framerate", str(out_fps),
            "-i", os.path.join(frames_out, "frame%08d.png"),
            "-i", src,
            "-map", "0:v", "-map", "1:a?",
            "-c:v", "libx264", "-crf", "17", "-preset", "fast",
            "-c:a", "copy",
            dst,
        ], check=True, capture_output=True)

    job.update(progress=97, message="RIFE interpolation complete")
