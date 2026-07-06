"""Frame interpolation -- fill in missing frames to smooth jerky footage.

Modes:
  auto    -- pick rife or mci automatically based on the clip's size (default)
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
import threading
import time
from pathlib import Path

from core.ffmpeg_utils import probe_file

log = logging.getLogger(__name__)

RIFE_SEARCH_PATHS = [
    r"C:\rife-ncnn-vulkan\rife-ncnn-vulkan.exe",
    r"C:\tools\rife-ncnn-vulkan\rife-ncnn-vulkan.exe",
]

# RIFE (ncnn-vulkan) can't read video -- it explodes every frame to PNG on disk,
# interpolates, then re-encodes. That temp footprint scales with frames x
# resolution and gets enormous for long/high-res clips (a 6-min 4K-ish clip
# needs hundreds of GB). Above this estimated peak, "auto" picks mci instead,
# which streams through ffmpeg with no PNG detour.
_RIFE_TEMP_BUDGET_GB = 20.0
# Keep at least this much disk free after the extract, whatever the estimate says.
_RIFE_DISK_HEADROOM_GB = 15.0
# Bytes per pixel for a photographic 8-bit PNG (measured ~2 MB for a 1080p frame).
_PNG_BYTES_PER_PX = 1.6


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


def _estimate_rife_temp_gb(info: dict, src_fps: float, multiplier: int) -> float:
    """Estimate RIFE's peak temp-disk use: input PNGs + interpolated output PNGs."""
    W = info.get("width") or 1920
    H = info.get("height") or 1080
    dur = info.get("duration") or 0.0
    n_in = int(dur * src_fps)
    if n_in <= 0:
        return float("inf")  # can't size it -> treat as too big for RIFE
    total_frames = n_in + n_in * multiplier
    return total_frames * W * H * _PNG_BYTES_PER_PX / 1e9


def _auto_mode(info: dict, src_fps: float, target_fps: float) -> str:
    """Choose 'rife' when its frame-extraction footprint fits comfortably on disk,
    otherwise 'mci'. Falls back to mci whenever RIFE isn't installed."""
    if _find_rife() is None:
        return "mci"
    multiplier = max(2, round((target_fps or src_fps * 2) / src_fps))
    est_gb = _estimate_rife_temp_gb(info, src_fps, multiplier)
    try:
        free_gb = shutil.disk_usage(tempfile.gettempdir()).free / 1e9
    except Exception:
        free_gb = 0.0
    fits_budget = est_gb <= _RIFE_TEMP_BUDGET_GB
    fits_disk = (free_gb - est_gb) >= _RIFE_DISK_HEADROOM_GB
    if fits_budget and fits_disk:
        log.info("[interpolate] auto -> rife (est temp %.1f GB, %.0f GB free)", est_gb, free_gb)
        return "rife"
    reason = "footprint" if not fits_budget else "low disk"
    log.info("[interpolate] auto -> mci (%s: est temp %.1f GB, %.0f GB free)",
             reason, est_gb, free_gb)
    return "mci"


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

    if mode == "auto":
        mode = _auto_mode(info, src_fps, target_fps)

    job.update(progress=5, message=f"Interpolating {Path(src).name} {src_fps:.1f}->{target_fps:.0f}fps ({mode})")

    if mode == "rife":
        rife = _find_rife()
        if rife:
            _run_rife(job, rife, src, dst, src_fps, target_fps, info.get("duration") or 0.0)
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

    from core.ffmpeg_utils import video_encode_args
    cmd = [
        "ffmpeg", "-y", "-i", src,
        "-vf", mi_filter,
        *video_encode_args(crf=17),
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


def _ffmpeg_with_progress(cmd, job, lo, hi, duration, label) -> None:
    """Run an ffmpeg command, mapping its time= readout onto progress [lo, hi] and
    keeping `label` as the status message so the bar advances instead of freezing."""
    job.update(progress=lo, message=label)
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    tail = collections.deque(maxlen=12)
    for line in proc.stdout:
        if job.stop_event.is_set():
            proc.terminate()
            raise RuntimeError("Cancelled")
        line = line.strip()
        if line:
            tail.append(line)
        if "time=" in line and duration:
            try:
                t = line.split("time=")[1].split()[0]
                h, m, s = t.split(":")
                elapsed = int(h) * 3600 + int(m) * 60 + float(s)
                frac = max(0.0, min(1.0, elapsed / duration))
                job.update(progress=int(lo + (hi - lo) * frac), message=label)
            except Exception:
                pass
    proc.wait()
    if proc.returncode != 0:
        detail = " | ".join(tail) or "no output captured"
        raise RuntimeError(f"{label}: ffmpeg failed -- {detail[-400:]}")


def _run_rife_gpu(job, rife_exe, frames_in, frames_out, target_count, lo, hi, multiplier) -> None:
    """Run rife-ncnn-vulkan (the GPU stage). It emits no parseable progress, so we
    report by watching the output folder fill toward `target_count` frames."""
    proc = subprocess.Popen(
        [rife_exe, "-i", frames_in, "-o", frames_out,
         "-m", "rife-v4.6", "-n", str(target_count), "-f", "frame%08d.png"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    tail = collections.deque(maxlen=6)

    def _drain():  # keep the pipe from filling and deadlocking rife
        try:
            for line in proc.stdout:
                line = line.strip()
                if line:
                    tail.append(line)
        except Exception:
            pass

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()

    while proc.poll() is None:
        if job.stop_event.is_set():
            proc.terminate()
            raise RuntimeError("Cancelled")
        try:
            done = sum(1 for e in os.scandir(frames_out) if e.name.endswith(".png"))
        except FileNotFoundError:
            done = 0
        frac = min(1.0, done / target_count) if target_count else 0.0
        job.update(progress=int(lo + (hi - lo) * frac),
                   message=f"RIFE interpolating on GPU (x{multiplier})... {int(frac * 100)}%")
        time.sleep(1.0)

    reader.join(timeout=2)
    if proc.returncode != 0:
        detail = " | ".join(tail) or "no output captured"
        raise RuntimeError("RIFE failed: " + detail[-400:])


def _run_rife(job, rife_exe: str, src: str, dst: str, src_fps: float,
              target_fps: float, duration: float = 0.0) -> None:
    multiplier = round(target_fps / src_fps)
    if multiplier < 2:
        multiplier = 2

    from core.ffmpeg_utils import video_encode_args

    with tempfile.TemporaryDirectory() as tmp:
        frames_in = os.path.join(tmp, "in")
        frames_out = os.path.join(tmp, "out")
        os.makedirs(frames_in)
        os.makedirs(frames_out)

        # Stage 1/3 -- extract every source frame to PNG. This is CPU/disk-bound;
        # the GPU stays idle here, which is expected (it only works in stage 2).
        _ffmpeg_with_progress(
            ["ffmpeg", "-y", "-i", src, "-q:v", "2",
             os.path.join(frames_in, "frame%08d.png")],
            job, 8, 33, duration,
            "Extracting frames (CPU) -- GPU engages at the RIFE step",
        )
        if job.stop_event.is_set():
            raise RuntimeError("Cancelled")

        # rife-ncnn-vulkan's -n is the TOTAL target frame count (not a multiplier),
        # and it names output frames by -f pattern (default %08d.png). Feed it the
        # real target count and the same "frame%08d.png" naming the re-encode reads.
        n_in = sum(1 for f in os.listdir(frames_in) if f.lower().endswith(".png"))
        if n_in < 2:
            raise RuntimeError("Video has too few frames to interpolate.")
        target_count = n_in * multiplier

        # Stage 2/3 -- the actual GPU-accelerated interpolation.
        _run_rife_gpu(job, rife_exe, frames_in, frames_out, target_count, 33, 80, multiplier)
        if job.stop_event.is_set():
            raise RuntimeError("Cancelled")

        # Stage 3/3 -- re-encode the interpolated frames back into a video.
        out_fps = src_fps * multiplier
        _ffmpeg_with_progress(
            ["ffmpeg", "-y", "-framerate", str(out_fps),
             "-i", os.path.join(frames_out, "frame%08d.png"),
             "-i", src, "-map", "0:v", "-map", "1:a?",
             *video_encode_args(crf=17), "-c:a", "copy", dst],
            job, 80, 97, duration,
            "Re-encoding smoothed video",
        )

    job.update(progress=97, message="RIFE interpolation complete")
