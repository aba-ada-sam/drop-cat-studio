"""
Music Mixer — FFmpeg-based audio mixing utility.
Mixes background music under video audio (ducked at -18dBFS relative to dialogue).
Loops music if shorter than video. Trims if longer.
"""

import os
import subprocess
import tempfile
import shutil
from pathlib import Path


def _run_tracked_process(cmd, timeout, active_procs=None, procs_lock=None,
                         capture_output=False):
    """Run ffmpeg while optionally registering the process for Stop-button kills."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
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


def _ffprobe_duration(path: str) -> float:
    """Get duration in seconds using ffprobe."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except Exception:
        pass
    return 0.0


def _ffprobe_has_audio(path: str) -> bool:
    """Check if the video file has an audio stream."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30
        )
        return bool(r.stdout.strip())
    except Exception:
        return False


def mix_music_under_video(
    video_path: str,
    music_path: str,
    output_path: str | None = None,
    music_volume_db: float = -18.0,
    dialogue_volume_db: float = 0.0,
    stop_check=None,
    active_procs=None,
    procs_lock=None,
) -> dict:
    """
    Mix background music under video audio using ffmpeg.

    Music is looped if shorter than video, trimmed if longer.
    Music is attenuated by music_volume_db relative to original.

    Args:
        video_path: Path to the input video (mp4).
        music_path: Path to the background music (mp3/wav).
        output_path: Where to save the output. Defaults to video_path stem + '_with_music.mp4'.
        music_volume_db: How many dB to attenuate the music (negative = quieter). Default -18.
        dialogue_volume_db: Volume adjustment for original video audio. Default 0 (unchanged).

    Returns:
        dict with keys: success, output_path, error
    """
    result = {"success": False, "output_path": None, "error": None}

    if not shutil.which("ffmpeg"):
        result["error"] = "ffmpeg not found on PATH"
        return result

    if not os.path.exists(video_path):
        result["error"] = f"Video not found: {video_path}"
        return result

    if not os.path.exists(music_path):
        result["error"] = f"Music not found: {music_path}"
        return result

    video_dur = _ffprobe_duration(video_path)
    music_dur = _ffprobe_duration(music_path)

    if video_dur <= 0:
        result["error"] = f"Could not determine video duration: {video_path}"
        return result

    if music_dur <= 0:
        result["error"] = f"Could not determine music duration: {music_path}"
        return result

    if output_path is None:
        stem = os.path.splitext(video_path)[0]
        output_path = stem + "_with_music.mp4"

    # Temp output to avoid overwriting input in-place
    tmp_out = output_path + ".tmp.mp4"
    has_video_audio = _ffprobe_has_audio(video_path)

    try:
        if stop_check and stop_check():
            result["error"] = "stopped"
            return result

        # Build ffmpeg filter graph
        # Strategy:
        #   - Loop music with -stream_loop -1, trim to video duration
        #   - Apply volume reduction to music
        #   - If video has audio: mix music + video audio with amix
        #   - If no video audio: just add music as-is

        music_vol = 10 ** (music_volume_db / 20.0)  # dB → linear
        dialogue_vol = 10 ** (dialogue_volume_db / 20.0)

        if has_video_audio:
            # Mix: [music looped + attenuated] + [video audio]
            filter_complex = (
                f"[1:a]volume={music_vol:.4f},atrim=0:{video_dur:.3f},asetpts=PTS-STARTPTS[music];"
                f"[0:a]volume={dialogue_vol:.4f}[dialogue];"
                f"[dialogue][music]amix=inputs=2:duration=first:dropout_transition=3[aout]"
            )
            cmd = [
                "ffmpeg", "-y",
                "-i", video_path,
                "-stream_loop", "-1", "-i", music_path,
                "-filter_complex", filter_complex,
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                tmp_out,
            ]
        else:
            # No original audio — just add music
            filter_complex = (
                f"[1:a]volume={music_vol:.4f},atrim=0:{video_dur:.3f},asetpts=PTS-STARTPTS[music]"
            )
            cmd = [
                "ffmpeg", "-y",
                "-i", video_path,
                "-stream_loop", "-1", "-i", music_path,
                "-filter_complex", filter_complex,
                "-map", "0:v", "-map", "[music]",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                tmp_out,
            ]

        print(f"[mixer] Mixing music into video ({video_dur:.1f}s video, {music_dur:.1f}s music loop)...", flush=True)
        r = _run_tracked_process(
            cmd,
            timeout=300,
            active_procs=active_procs,
            procs_lock=procs_lock,
            capture_output=True,
        )

        if r.returncode != 0:
            err = r.stderr.decode(errors="replace")[-400:] if r.stderr else ""
            result["error"] = f"ffmpeg mix failed: {err}"
            print(f"[mixer] {result['error']}", flush=True)
            return result

        # Rename tmp to final
        if os.path.exists(output_path):
            os.remove(output_path)
        shutil.move(tmp_out, output_path)

        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"[mixer] Done: {os.path.basename(output_path)} ({size_mb:.1f} MB)", flush=True)
        result["success"] = True
        result["output_path"] = output_path

    except subprocess.TimeoutExpired:
        result["error"] = "ffmpeg mix timed out"
        print(f"[mixer] {result['error']}", flush=True)
    except Exception as e:
        result["error"] = f"Mix error: {e}"
        print(f"[mixer] {result['error']}", flush=True)
    finally:
        if os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except Exception:
                pass

    return result
