"""MuseTalk lip-sync runner.

Drives a dedicated, isolated MuseTalk install (default C:\\MuseTalk) as a
one-shot subprocess: given a video + driving audio, MuseTalk re-renders the
mouth region to match the audio and muxes the audio back in. The MuseTalk venv
is separate from Forge/DCS and carries the Blackwell/Windows fixes baked in:
  - sitecustomize.py forces torch.load(weights_only=False) for legacy ckpts
  - TORCHDYNAMO_DISABLE=1  (no Triton on Windows)
  - PYTHONUTF8=1           (MuseTalk prints non-ASCII chars)
  - mmpose swapped for face-alignment in musetalk/utils/preprocessing.py, and
    the face box derived from those landmarks (the human-only SFD detector
    missed creature faces -> only ~13/776 frames processed)
  - scripts/inference.py writes the ORIGINAL frame for any skipped index so the
    %08d PNG sequence stays contiguous (gaps made ffmpeg's image2 reader stop at
    the first miss -> 27/776 frames = near-frozen "no lip sync")

For a song, the full mix is poor sync material for the speech-trained model, so
Demucs isolates the vocal stem to drive the mouth and the original song is
re-muxed for the final (see isolate_vocals).

Isolating the vocals is not enough on its own. A 3:30 song is typically only
~50% singing -- intro, solos, outro and the gaps between phrases are all
instrumental -- and the Demucs stem still carries audible bleed (kick, cymbals,
guitar) right through them. MuseTalk turns that bleed into mouth movement, which
is what makes a character "sing to the background music". So we detect the
intervals that actually contain singing and use them twice:

  1. the driving audio is hard-gated to silence outside them, and
  2. the ORIGINAL frames are composited back over those stretches,

so between phrases the mouth is provably untouched rather than merely quiet.

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
from core.ffmpeg_utils import probe_duration, probe_file, run_ffmpeg, video_encode_args

from .vocal_activity import enable_expr, gate_audio, voiced_intervals

log = logging.getLogger(__name__)


class NoVocalsError(RuntimeError):
    """The driving track has no singing to sync to (fully instrumental)."""

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


def _separate_vocals(musetalk_py: Path, in_audio: str, out_vocals: str) -> bool:
    """Isolate the vocal stem via Demucs, run in the MuseTalk venv (has demucs)."""
    helper = Path(__file__).resolve().parent / "_demucs_separate.py"
    env = dict(os.environ)
    env["TORCHDYNAMO_DISABLE"] = "1"
    env["PYTHONUTF8"] = "1"
    try:
        r = subprocess.run(
            [str(musetalk_py), str(helper), in_audio, out_vocals],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, env=env,
        )
        if r.returncode != 0:
            log.warning("[lipsync] demucs failed:\n%s", (r.stderr or "")[-800:])
        return r.returncode == 0 and os.path.isfile(out_vocals)
    except Exception as e:
        log.warning("[lipsync] demucs exception: %s", e)
        return False


def _composite_voiced(base_video: str, synced_video: str,
                      intervals: list[tuple[float, float]], out_path: str) -> bool:
    """Show `synced_video` only while someone is singing, `base_video` otherwise.

    MuseTalk re-renders the mouth for every frame it is given, including the
    instrumental bars where the gated driving audio is silent. Overlaying its
    output onto the original video, enabled only inside the vocal intervals,
    makes the mouth exactly as still as the source between phrases.
    """
    info = probe_file(base_video)
    w, h = info.get("width"), info.get("height")
    if not w or not h:
        log.warning("[lipsync] could not probe %s -- skipping composite", base_video)
        return False

    # eof_action=pass + repeatlast=0: if MuseTalk's track ends early, the base
    # video plays on rather than freezing on its last synced frame.
    graph = (
        f"[0:v]setpts=PTS-STARTPTS[b];"
        f"[1:v]setpts=PTS-STARTPTS,scale={w}:{h}[s];"
        f"[b][s]overlay=eof_action=pass:repeatlast=0:enable='{enable_expr(intervals)}'[v]"
    )
    r = run_ffmpeg(
        ["ffmpeg", "-y", "-i", base_video, "-i", synced_video,
         "-filter_complex", graph, "-map", "[v]", "-an",
         *video_encode_args(crf=18), "-movflags", "+faststart", out_path],
        timeout=1800,
    )
    if r.returncode != 0 or not os.path.isfile(out_path):
        err = (r.stderr or b"").decode("utf-8", "replace")[-800:]
        log.warning("[lipsync] composite failed:\n%s", err)
        return False
    return True


def _remux_audio(video_in: str, audio_in: str, out_path: str) -> bool:
    """Replace the video's audio with audio_in (full song), keeping the video stream."""
    r = run_ffmpeg(
        ["ffmpeg", "-y", "-i", video_in, "-i", audio_in,
         "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-movflags", "+faststart", out_path],
        timeout=300,
    )
    return r.returncode == 0 and os.path.isfile(out_path)


def lipsync_video(job, video_path: str, audio_path: str | None, out_path: str,
                  bbox_shift: int = 0, isolate_vocals: bool | None = None) -> str:
    """Lip-sync video_path to audio_path (or the video's own audio). Returns out_path.

    When isolate_vocals (default from config lipsync_isolate_vocals), Demucs
    extracts the vocal stem to DRIVE MuseTalk (a song's full mix is poor sync
    material for the speech-trained model), then the ORIGINAL full audio is
    re-muxed onto the result so the final video plays the whole song.
    """
    d, py = _paths()
    if not lipsync_available():
        raise RuntimeError(
            "MuseTalk is not installed. Expected the venv + models under "
            f"{d} (set musetalk_dir / musetalk_python in config)."
        )
    if isolate_vocals is None:
        isolate_vocals = bool(cfg.get("lipsync_isolate_vocals", True))

    job.update(progress=6, message="Preparing lip-sync...")

    # MuseTalk needs the GPU to itself; evict WanGP/ACE-Step/Forge/Ollama.
    try:
        from core.gpu_orchestrator import gpu
        gpu.release_all()
    except Exception as e:
        log.warning("[lipsync] gpu release_all failed (continuing): %s", e)

    work = d / "results" / f"dcs_{job.id[:8]}"
    work.mkdir(parents=True, exist_ok=True)
    cfg_path = d / "configs" / "inference" / f"_dcs_{job.id[:8]}.yaml"

    # The full audio that plays in the FINAL video (provided file or the video's
    # own track).
    original_audio = audio_path
    if not original_audio or not os.path.isfile(original_audio):
        original_audio = _extract_audio(video_path, str(work / "_orig.wav"))
        if not original_audio:
            raise RuntimeError("No audio provided and could not extract audio from the video")

    # What MuseTalk's mouth syncs to: isolated vocals (better) or the full audio.
    # `intervals` stays None when we never got a clean stem to analyse, which
    # also disables the composite below -- we only claim to know where the
    # singing is when we actually looked at a vocal stem.
    drive_audio = original_audio
    intervals: list[tuple[float, float]] | None = None
    if isolate_vocals:
        job.update(progress=10, message="Isolating vocals...")
        voc = str(work / "_vocals.wav")
        if _separate_vocals(py, original_audio, voc):
            drive_audio = voc

            job.update(progress=14, message="Finding the sung phrases...")
            intervals = voiced_intervals(voc)
            if not intervals:
                shutil.rmtree(work, ignore_errors=True)
                raise NoVocalsError(
                    "No singing found in this track -- it is instrumental, so there "
                    "are no words to lip-sync to."
                )
            sung = sum(e - s for s, e in intervals)
            total = probe_duration(voc) or 0.0
            if total and sung < 0.98 * total:
                gated = str(work / "_vocals_gated.wav")
                if gate_audio(voc, intervals, gated):
                    drive_audio = gated
                    log.info(
                        "[lipsync] driving on %d sung phrases (%.0fs of %.0fs); "
                        "the other %.0fs is silenced so the mouth rests",
                        len(intervals), sung, total, total - sung,
                    )
            else:
                intervals = None      # sung end to end: nothing to composite back
        else:
            log.warning("[lipsync] vocal isolation failed -- driving with full audio")

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
        tail = (proc.stdout or "")[-1500:]
        log.error("[lipsync] no output mp4. stdout tail:\n%s", tail)
        # MuseTalk swallows its own exceptions and exits 0, so the real cause is
        # only ever in stdout. Quote it instead of guessing "no face detected".
        why = next(
            (ln.strip() for ln in reversed(tail.splitlines())
             if "Error occurred during processing" in ln),
            "",
        )
        raise RuntimeError(
            f"MuseTalk produced no synced video -- {why}" if why else
            "MuseTalk produced no synced video -- usually means no face was "
            "detected in the input. Lip-sync needs a clear frontal face."
        )
    src = max(finals, key=lambda p: p.stat().st_mtime)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    # Put the original frames back wherever nobody is singing.
    video_src = str(src)
    if intervals:
        job.update(progress=84, message="Restoring the mouth between phrases...")
        comp = str(work / "_composited.mp4")
        if _composite_voiced(video_path, str(src), intervals, comp):
            video_src = comp
        else:
            log.warning("[lipsync] composite failed -- mouth may move on instrumental bars")

    if video_src != str(src) or drive_audio != original_audio:
        # The composite is video-only, and MuseTalk muxed whatever we drove it
        # with. Either way the final needs the full song back (same timeline, so
        # the mouth still matches).
        job.update(progress=88, message="Restoring full song audio...")
        if not _remux_audio(video_src, original_audio, out_path):
            log.warning("[lipsync] re-mux failed -- keeping the driving audio")
            shutil.move(video_src, out_path)
    else:
        shutil.move(video_src, out_path)

    # Cleanup the MuseTalk work dir + temp config.
    try:
        shutil.rmtree(work, ignore_errors=True)
        cfg_path.unlink(missing_ok=True)
    except Exception:
        pass

    dur = probe_duration(out_path)
    job.update(progress=92, message=f"Lip-sync done ({dur:.1f}s)" if dur else "Lip-sync done")
    return out_path
