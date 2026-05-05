"""Motion-peak detection and beat-aligned speed-ramping for song-video clips.

The song-video pipeline generates clips whose *internal* motion peaks land
wherever the AI decides — usually not on the audio's beat. This module fixes
that by:

  1. Detecting WHERE inside each clip the visual action peaks (frame diff)
  2. Time-warping the clip with a piecewise-linear speed ramp so that the
     natural motion peak gets pulled onto the target beat timestamp.

The ramp preserves total clip duration: the segment before the natural peak
plays at one speed, the segment after at a different speed, and they meet at
the target time. Ratios are clamped so the warp stays musically natural —
clips that would need extreme speedup/slowdown are returned untouched.

Optional dependency: opencv-python. If cv2 is missing, find_motion_peak()
returns None and the pipeline degrades to plain hard-cut concat (current
behavior pre-feature).
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def find_motion_peak(video_path: str) -> tuple[float, float] | None:
    """Locate the timestamp of peak visual motion in a video.

    Uses frame differencing (mean absolute pixel delta between consecutive
    downsampled frames) smoothed with a 0.4 s moving average. The smoothing
    suppresses single-frame spikes (compression artifacts, scene noise) so
    the returned peak corresponds to a sustained motion event rather than a
    pixel glitch.

    Returns (peak_time_seconds, normalized_confidence) or None on failure.
    Confidence is the ratio peak_value / mean_value — values >= 1.3 indicate
    a clearly defined peak; below that the clip is roughly uniform motion
    and ramping it provides little benefit (see align_clip_to_beat min_confidence).
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        log.info("[motion-analyzer] opencv-python not installed — skipping motion peak detection")
        return None

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        log.warning("[motion-analyzer] Could not open %s", video_path)
        return None

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    motion: list[float] = []
    prev_gray = None

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            small = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            if prev_gray is None:
                motion.append(0.0)
            else:
                diff = cv2.absdiff(gray, prev_gray)
                motion.append(float(np.mean(diff)))
            prev_gray = gray
    finally:
        cap.release()

    if len(motion) < 5:
        return None

    arr = np.array(motion, dtype=np.float32)
    win = max(3, int(round(fps * 0.4)))
    kernel = np.ones(win, dtype=np.float32) / win
    smoothed = np.convolve(arr, kernel, mode="same")

    peak_frame = int(np.argmax(smoothed))
    peak_time = peak_frame / fps

    mean_motion = float(np.mean(smoothed)) or 1e-6
    confidence = float(smoothed[peak_frame]) / mean_motion

    log.debug(
        "[motion-analyzer] %s: peak at %.2fs (frame %d, %d total, conf=%.2f)",
        Path(video_path).name, peak_time, peak_frame, len(motion), confidence,
    )
    return peak_time, confidence


def speed_ramp_to_target(
    video_path: str,
    out_path: str,
    t_natural: float,
    t_target: float,
    dur: float,
    max_ratio: float = 1.8,
) -> bool:
    """Apply piecewise linear speed ramp so motion peak at t_natural lands at t_target.

    Math:
      Segment 1 [0, t_natural] is replayed in [0, t_target]
        speed factor s1 = t_target / t_natural
        (s1 < 1 → segment plays faster; > 1 → slower)
      Segment 2 [t_natural, dur] is replayed in [t_target, dur]
        speed factor s2 = (dur - t_target) / (dur - t_natural)

    Both factors are required to be in [1/max_ratio, max_ratio]. If either
    falls outside, the requested warp would distort motion unacceptably and
    we return False so the caller keeps the original clip.

    ffmpeg setpts semantics: setpts=PTS*X stretches duration by X.
    X<1 → faster output, X>1 → slower output.
    """
    edge = 0.4  # don't ramp peaks too close to clip edges (no room to warp)
    if (
        t_natural < edge
        or t_target < edge
        or t_natural > dur - edge
        or t_target > dur - edge
        or dur < 2 * edge
    ):
        return False

    s1 = t_target / t_natural
    s2 = (dur - t_target) / (dur - t_natural)

    min_ratio = 1.0 / max_ratio
    if not (min_ratio <= s1 <= max_ratio and min_ratio <= s2 <= max_ratio):
        log.debug(
            "[motion-analyzer] ramp out of bounds: s1=%.2f s2=%.2f (clamp %.2f-%.2f) — keeping original",
            s1, s2, min_ratio, max_ratio,
        )
        return False

    # Filter complex: split by trim, rescale each half's PTS, concat.
    # The setpts=PTS-STARTPTS resets the trim's PTS to zero before scaling.
    fc = (
        f"[0:v]trim=0:{t_natural:.3f},setpts=PTS-STARTPTS,setpts={s1:.5f}*PTS[a];"
        f"[0:v]trim={t_natural:.3f}:{dur:.3f},setpts=PTS-STARTPTS,setpts={s2:.5f}*PTS[b];"
        f"[a][b]concat=n=2:v=1[out]"
    )
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-filter_complex", fc,
        "-map", "[out]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-an",
        out_path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=180)
        if r.returncode == 0 and Path(out_path).exists():
            log.info(
                "[motion-analyzer] ramp ok: %.2fs→%.2fs in %.1fs clip (s1=%.2f, s2=%.2f)",
                t_natural, t_target, dur, s1, s2,
            )
            return True
        log.warning(
            "[motion-analyzer] ramp ffmpeg failed (rc=%d): %s",
            r.returncode, r.stderr.decode(errors="replace")[-400:],
        )
    except Exception as e:
        log.warning("[motion-analyzer] ramp exception: %s", e)
    return False


def align_clip_to_beat(
    clip_path: str,
    target_time: float,
    clip_duration: float,
    out_path: str,
    min_confidence: float = 1.3,
) -> tuple[bool, dict]:
    """High-level wrapper: find the natural motion peak, ramp it onto the beat.

    Returns (applied, info_dict). info_dict carries diagnostic fields the
    pipeline can surface in its progress messages and logs:
        natural_time   — detected peak time in seconds (may be None)
        target_time    — caller-supplied beat time
        confidence     — peak/mean motion ratio (>1.3 is a clean peak)
        applied        — True if speed ramp was actually written
        reason         — human-readable explanation when applied=False

    The caller decides what to do with applied=False clips — generally it
    should keep the original generated clip unmodified.
    """
    info: dict = {
        "natural_time": None,
        "target_time": target_time,
        "confidence": None,
        "applied": False,
        "reason": "",
    }
    peak = find_motion_peak(clip_path)
    if peak is None:
        info["reason"] = "no peak detected (cv2 unavailable or clip too short)"
        return False, info

    t_nat, conf = peak
    info["natural_time"] = t_nat
    info["confidence"] = conf

    if conf < min_confidence:
        info["reason"] = f"motion is uniform (conf={conf:.2f}) — ramping won't help"
        return False, info

    if abs(t_nat - target_time) < 0.25:
        info["reason"] = "peak already on beat"
        return False, info

    ok = speed_ramp_to_target(clip_path, out_path, t_nat, target_time, clip_duration)
    info["applied"] = ok
    if not ok and not info["reason"]:
        info["reason"] = "ramp ratio out of bounds or ffmpeg error"
    return ok, info
