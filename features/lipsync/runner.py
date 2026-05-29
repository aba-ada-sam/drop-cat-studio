"""MuseTalk lip-sync runner.

Drives a dedicated, isolated MuseTalk install (default C:\\MuseTalk) as a
one-shot subprocess: given a video + driving audio, MuseTalk re-renders the
mouth region to match the audio and muxes the audio back in. The MuseTalk venv
is separate from Forge/DCS and carries the Blackwell/Windows fixes baked in:
  - sitecustomize.py forces torch.load(weights_only=False) for legacy ckpts
  - TORCHDYNAMO_DISABLE=1  (no Triton on Windows)
  - PYTHONUTF8=1           (MuseTalk prints non-ASCII chars)
  - mmpose swapped for face-alignment in musetalk/utils/preprocessing.py

Limitation: mouth-sync needs a detectable frontal face. Abstract/non-face
content (e.g. a skull sculpture) won't sync.
"""
import datetime
import logging
import os
import shutil
import subprocess
from pathlib import Path

from core import config as cfg
from core.ffmpeg_utils import probe_duration, run_ffmpeg

log = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"
_DEFAULT_DIR = r"C:\MuseTalk"


def _paths():
    d = Path(cfg.get("musetalk_dir") or _DEFAULT_DIR)
    py = cfg.get("musetalk_python") or str(d / "venv" / "Scripts" / "python.exe")
    return d, Path(py)


def lipsync_available() -> bool:
    """True if the MuseTalk install + venv + v15 weights are present."""
    d, py = _paths()
    return (
        py.is_file()
        and (d / "scripts" / "inference.py").is_file()
        and (d / "models" / "musetalkV15" / "unet.pth").is_file()
    )


def _extract_audio(video_path: str, out_wav: str) -> str | None:
    r = run_ffmpeg(
        ["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", out_wav],
        timeout=120,
    )
    return out_wav if r.returncode == 0 and os.path.isfile(out_wav) else None


def lipsync_video(job, video_path: str, audio_path: str | None, out_path: str, bbox_shift: int = 0) -> str:
    """Lip-sync video_path to audio_path (or the video's own audio). Returns out_path."""
    d, py = _paths()
    if not lipsync_available():
        raise RuntimeError(
            "MuseTalk is not installed. Expected the venv + models under "
            f"{d} (set musetalk_dir / musetalk_python in config)."
        )

    job.update(progress=6, message="Preparing lip-sync...")

    # MuseTalk needs the GPU to itself; evict WanGP/ACE-Step/Forge/Ollama.
    try:
        from core.gpu_orchestrator import gpu
        gpu.release_all()
    except Exception as e:
        log.warning("[lipsync] gpu release_all failed (continuing): %s", e)

    work = d / "results" / f"dcs_{job.id[:8]}"
    cfg_path = d / "configs" / "inference" / f"_dcs_{job.id[:8]}.yaml"

    # Driving audio: use the provided file, else extract the video's own track.
    drive_audio = audio_path
    tmp_wav = None
    if not drive_audio or not os.path.isfile(drive_audio):
        tmp_wav = str(work / "_drive.wav")
        work.mkdir(parents=True, exist_ok=True)
        drive_audio = _extract_audio(video_path, tmp_wav)
        if not drive_audio:
            raise RuntimeError("No audio provided and could not extract audio from the video")

    cfg_yaml = (
        "task_0:\n"
        f' video_path: "{video_path.replace(chr(92), "/")}"\n'
        f' audio_path: "{drive_audio.replace(chr(92), "/")}"\n'
    )
    if bbox_shift:
        cfg_yaml += f" bbox_shift: {int(bbox_shift)}\n"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(cfg_yaml, encoding="utf-8")

    env = dict(os.environ)
    env["TORCHDYNAMO_DISABLE"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    cmd = [
        str(py), "-m", "scripts.inference",
        "--inference_config", str(cfg_path),
        "--result_dir", str(work),
        "--unet_model_path", "models/musetalkV15/unet.pth",
        "--unet_config", "models/musetalkV15/musetalk.json",
        "--version", "v15",
    ]
    job.update(progress=20, message="Running MuseTalk lip-sync...")
    try:
        proc = subprocess.run(
            cmd, cwd=str(d), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=1800,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("MuseTalk timed out (>30 min)")

    if proc.returncode != 0:
        log.error("[lipsync] inference failed:\n%s", (proc.stderr or "")[-1800:])
        raise RuntimeError("MuseTalk inference failed -- see server log")

    # Output lands at <work>/v15/<videostem>_<audiostem>.mp4 (temp_* is the
    # video-only intermediate -- skip it).
    finals = [p for p in (work / "v15").glob("*.mp4") if not p.name.startswith("temp_")]
    if not finals:
        log.error("[lipsync] no output mp4. stdout tail:\n%s", (proc.stdout or "")[-1500:])
        raise RuntimeError(
            "MuseTalk produced no synced video -- usually means no face was "
            "detected in the input. Lip-sync needs a clear frontal face."
        )
    src = max(finals, key=lambda p: p.stat().st_mtime)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), out_path)

    # Cleanup the MuseTalk work dir + temp config.
    try:
        shutil.rmtree(work, ignore_errors=True)
        cfg_path.unlink(missing_ok=True)
    except Exception:
        pass

    dur = probe_duration(out_path)
    job.update(progress=92, message=f"Lip-sync done ({dur:.1f}s)" if dur else "Lip-sync done")
    return out_path
