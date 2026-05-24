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
    """Return motion peaks and clip boundaries from a video file using ffmpeg."""
    from core.ffmpeg_utils import probe_duration
    duration = probe_duration(video_path) or 0.0

    # Detect scene changes (hard cuts) via showinfo
    boundaries = [0.0]
    try:
        r = subprocess.run(
            ["ffmpeg", "-i", video_path,
             "-vf", "select='gt(scene,0.25)',showinfo",
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
        log.warning("[beat_sync] Scene detection failed: %s", e)

    # Detect motion peaks via frame difference (use select filter with scene score metadata)
    motion_peaks = []
    try:
        r = subprocess.run(
            ["ffmpeg", "-i", video_path,
             "-vf", "select='gte(scene,0.05)',showinfo,metadata=mode=print",
             "-f", "null", "-"],
            capture_output=True, timeout=120, text=True, errors="replace",
        )
        scores = []
        times = []
        for line in r.stderr.splitlines():
            tm = re.search(r"pts_time:([\d.]+)", line)
            sm = re.search(r"scene_score=([\d.]+)", line)
            if tm and sm:
                times.append(float(tm.group(1)))
                scores.append(float(sm.group(1)))
        if scores:
            threshold = sorted(scores)[int(len(scores) * 0.6)]
            prev_t = -3.0
            for t, s in zip(times, scores):
                if s >= threshold and t - prev_t >= 1.5:
                    motion_peaks.append(round(t, 3))
                    prev_t = t
    except Exception as e:
        log.warning("[beat_sync] Motion peak detection failed: %s", e)

    return {
        "duration": duration,
        "motion_peaks": motion_peaks,
        "clip_boundaries": sorted(set(boundaries)),
    }
