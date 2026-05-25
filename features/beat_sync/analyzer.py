"""Beat-sync analyzer: audio beat peaks + video motion peaks."""
import logging
import subprocess
import re
import os
from pathlib import Path

log = logging.getLogger(__name__)


def analyze_audio_beats(audio_path: str) -> dict:
    """Return beat times, energy peaks, BPM, duration from an audio file."""
    try:
        from features.fun_videos.audio_analyzer import detect_audio_events
        return detect_audio_events(audio_path)
    except Exception as e:
        log.warning("[beat_sync] Audio analysis failed: %s", e)
        return {}


def analyze_video_motion(video_path: str) -> dict:
    """Return motion peaks and clip boundaries from a video file.

    Uses three methods in order:
    1. Frame-difference energy: detects where visual content changes significantly
       -- works for both hard cuts and soft transitions (xfade).
    2. Scene change detection at a lowered threshold: catches hard cuts.
    3. If both find nothing, falls back to evenly-spaced positions every 8s.
    """
    from core.ffmpeg_utils import probe_duration
    duration = probe_duration(video_path) or 0.0

    # Method 1: frame difference energy via signalstats + select
    # Computes per-frame average absolute difference -- more sensitive than
    # scene change score for soft transitions like xfade.
    motion_peaks = _detect_by_frame_diff(video_path, duration)

    # Method 2: scene change detection (hard cuts), lower threshold than before
    boundaries = _detect_scene_cuts(video_path, threshold=0.15)

    # Method 3: if frame diff found nothing, try the clip boundaries
    if not motion_peaks and boundaries:
        motion_peaks = [b for b in boundaries if b > 0.5]
        log.info("[beat_sync] No frame-diff peaks -- using %d scene cuts as motion peaks", len(motion_peaks))

    # Method 4: last resort -- evenly spaced every 8s
    if not motion_peaks and duration > 0:
        step = 8.0
        motion_peaks = [round(t, 2) for t in _frange(step, duration - step * 0.5, step)]
        log.info("[beat_sync] No peaks detected -- using %d evenly-spaced positions", len(motion_peaks))

    log.info("[beat_sync] video analysis: dur=%.1fs, boundaries=%d, motion_peaks=%d",
             duration, len(boundaries), len(motion_peaks))

    return {
        "duration": duration,
        "motion_peaks": sorted(set(round(t, 3) for t in motion_peaks)),
        "clip_boundaries": sorted(set([0.0] + [round(b, 3) for b in boundaries])),
    }


def _frange(start, stop, step):
    t = start
    while t < stop:
        yield t
        t += step


def _detect_by_frame_diff(video_path: str, duration: float) -> list:
    """Detect motion peaks via per-frame pixel-difference energy.

    Uses ffmpeg signalstats to get YDIF (luma frame difference) per frame,
    then picks local maxima above 75th percentile spaced at least 1s apart.
    Works on both hard cuts and soft xfade transitions.
    """
    if duration <= 0:
        return []
    try:
        # Extract YDIF metric: average absolute luma difference from previous frame
        r = subprocess.run(
            ["ffmpeg", "-i", video_path,
             "-vf", "signalstats=stat=YDIF",
             "-an", "-f", "null", "-"],
            capture_output=True, timeout=300, text=True, errors="replace",
        )
        # Parse "pts_time:X ... YDIF:Y" from stderr
        times, diffs = [], []
        for line in r.stderr.splitlines():
            tm = re.search(r"pts_time:([\d.]+)", line)
            yd = re.search(r"YDIF:([\d.]+)", line)
            if tm and yd:
                times.append(float(tm.group(1)))
                diffs.append(float(yd.group(1)))

        if not diffs:
            return []

        # Threshold at 75th percentile
        sorted_d = sorted(diffs)
        threshold = sorted_d[int(len(sorted_d) * 0.75)]

        # Pick local maxima above threshold, min 1s apart
        peaks = []
        prev_t = -2.0
        for t, d in zip(times, diffs):
            if d >= threshold and t - prev_t >= 1.0:
                peaks.append(round(t, 3))
                prev_t = t
        return peaks
    except Exception as e:
        log.warning("[beat_sync] Frame-diff detection failed: %s", e)
        return []


def _detect_scene_cuts(video_path: str, threshold: float = 0.15) -> list:
    """Detect hard cuts via ffmpeg scene change detection."""
    boundaries = []
    try:
        r = subprocess.run(
            ["ffmpeg", "-i", video_path,
             "-vf", f"select='gt(scene,{threshold})',showinfo",
             "-f", "null", "-"],
            capture_output=True, timeout=120, text=True, errors="replace",
        )
        for line in r.stderr.splitlines():
            m = re.search(r"pts_time:([\d.]+)", line)
            if m:
                t = float(m.group(1))
                if t > 0.1:
                    boundaries.append(round(t, 3))
    except Exception as e:
        log.warning("[beat_sync] Scene cut detection failed: %s", e)
    return boundaries
