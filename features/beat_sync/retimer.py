"""Post-generation video retimer: warp video sections to align motion peaks to audio beats."""
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from core.ffmpeg_utils import probe_duration

log = logging.getLogger(__name__)

_MIN_SPEED = 0.5   # never slow a section below 0.5x
_MAX_SPEED = 2.0   # never speed a section above 2x


def build_remap(video_dur: float, remap_points: list) -> list:
    """Convert sparse remap_points into a full piecewise segment list.

    remap_points: list of {video_t, target_t} -- "the event at video_t should
    land at target_t in the output."

    Returns list of {v_start, v_end, t_start, t_end, speed} covering [0, video_dur].
    """
    pts = sorted(remap_points, key=lambda p: p["video_t"])
    # Bookend with identity anchors at start and end
    anchors = [(0.0, 0.0)] + [(p["video_t"], p["target_t"]) for p in pts] + [(video_dur, video_dur)]
    segments = []
    for i in range(len(anchors) - 1):
        v0, t0 = anchors[i]
        v1, t1 = anchors[i + 1]
        v_span = v1 - v0
        t_span = t1 - t0
        if v_span <= 0 or t_span <= 0:
            continue
        speed = max(_MIN_SPEED, min(_MAX_SPEED, v_span / t_span))
        segments.append({"v_start": v0, "v_end": v1, "t_start": t0, "t_end": t1, "speed": speed})
    return segments


def retime_video(video_path: str, audio_path: str, out_path: str,
                 remap_points=None,
                 auto_align: bool = True):
    """Stretch/compress video sections to align motion peaks to audio beats.

    If remap_points is None and auto_align=True, automatically aligns the
    video's detected motion peaks to the nearest audio energy peaks.

    Returns (success, error_message).
    """
    from features.beat_sync.analyzer import analyze_audio_beats, analyze_video_motion

    video_dur = probe_duration(video_path)
    if not video_dur:
        return False, "Could not probe video duration"

    if remap_points is None and auto_align:
        audio_data = analyze_audio_beats(audio_path)
        video_data = analyze_video_motion(video_path)
        remap_points = _auto_remap(
            video_data.get("motion_peaks", []),
            audio_data.get("energy_peaks", []),
            audio_data.get("beat_times", []),
            video_dur,
            clip_boundaries=video_data.get("clip_boundaries", []),
        )

    if not remap_points:
        # No alignment needed -- just mux audio onto video
        return _mux_audio(video_path, audio_path, out_path)

    segments = build_remap(video_dur, remap_points)
    if not segments:
        return _mux_audio(video_path, audio_path, out_path)

    # Interpolate to 60fps for smooth slow-motion, then apply setpts per segment
    with tempfile.TemporaryDirectory() as tmp:
        interp_path = os.path.join(tmp, "interp.mp4")
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", video_path,
             "-vf", "minterpolate=fps=60:mi_mode=mci",
             "-an", "-c:v", "libx264", "-crf", "15", "-preset", "fast",
             interp_path],
            capture_output=True, timeout=600,
        )
        if r.returncode != 0 or not Path(interp_path).exists():
            log.warning("[retimer] minterpolate failed, falling back to direct mux")
            return _mux_audio(video_path, audio_path, out_path)

        # Build concat list with per-segment speed
        seg_files = []
        for j, seg in enumerate(segments):
            seg_out = os.path.join(tmp, f"seg_{j:03d}.mp4")
            speed = seg["speed"]
            pts_expr = f"PTS*{speed:.6f}"
            r2 = subprocess.run(
                ["ffmpeg", "-y", "-i", interp_path,
                 "-ss", f"{seg['v_start']:.4f}", "-to", f"{seg['v_end']:.4f}",
                 "-vf", f"setpts={pts_expr}",
                 "-an", "-c:v", "libx264", "-crf", "15", "-preset", "fast",
                 seg_out],
                capture_output=True, timeout=120,
            )
            if r2.returncode == 0 and Path(seg_out).exists():
                seg_files.append(seg_out)

        if not seg_files:
            return _mux_audio(video_path, audio_path, out_path)

        # Concat segments
        list_path = os.path.join(tmp, "concat.txt")
        with open(list_path, "w") as f:
            for sf in seg_files:
                f.write(f"file '{sf}'\n")
        concat_path = os.path.join(tmp, "concat.mp4")
        r3 = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
             "-c", "copy", concat_path],
            capture_output=True, timeout=300,
        )
        if r3.returncode != 0 or not Path(concat_path).exists():
            return _mux_audio(video_path, audio_path, out_path)

        return _mux_audio(concat_path, audio_path, out_path)


def _auto_remap(motion_peaks: list, energy_peaks: list, beat_times: list,
                video_dur: float, snap_window: float = 2.0,
                clip_boundaries: list | None = None) -> list:
    """Match each video motion/cut point to the nearest audio beat or energy peak.

    Falls back to aligning clip_boundaries if motion_peaks is empty.
    snap_window raised to 2.0s -- music videos rarely have tight sync to begin with,
    so a 1.5s window misses too many alignments.
    """
    # Use motion peaks if available, otherwise align clip cut points
    targets = [t for t in (motion_peaks or []) if 0 < t < video_dur]
    if not targets and clip_boundaries:
        targets = [t for t in clip_boundaries if 0 < t < video_dur]
        log.info("[retimer] No motion peaks -- aligning %d clip boundaries instead", len(targets))

    # Candidates: energy peaks + every 4th beat (downbeats) + every 2nd beat for denser coverage
    downbeats  = beat_times[::4] if beat_times else []
    halfbeats  = beat_times[::2] if beat_times else []
    candidates = sorted(set(list(energy_peaks) + downbeats + halfbeats))

    if not candidates:
        log.warning("[retimer] No audio candidates to align to")
        return []
    if not targets:
        log.warning("[retimer] No video targets to align (no peaks, no boundaries)")
        return []

    remap = []
    used = set()
    for vt in sorted(targets):
        best = min(candidates, key=lambda c: abs(c - vt))
        if abs(best - vt) <= snap_window and best not in used and 0 < best < video_dur:
            remap.append({"video_t": vt, "target_t": best})
            used.add(best)
    log.info("[retimer] Auto-remap: %d/%d targets aligned (snap_window=%.1fs)",
             len(remap), len(targets), snap_window)
    return remap


def _mux_audio(video_path: str, audio_path: str, out_path: str):
    """Mux audio onto video without retiming."""
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-i", audio_path,
         "-c:v", "copy", "-c:a", "aac", "-shortest", out_path],
        capture_output=True, timeout=300,
    )
    if r.returncode == 0 and Path(out_path).exists():
        return True, ""
    return False, r.stderr.decode(errors="replace")[-400:]
