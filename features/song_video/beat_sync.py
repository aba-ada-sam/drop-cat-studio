"""Beat-sync post-processing for assembled music videos.

After the video is fully assembled and audio merged, this module
squeezes/stretches the video timeline so scene changes land on
beat/onset peaks in the song -- using DTW alignment of the continuous
video-motion energy profile against the audio energy profile.

The audio track is left completely untouched; only the video PTS
mapping changes. A 3x max-speed clamp prevents extreme warping
that would make any segment look like a slideshow or blur.

Dependencies: librosa, scipy, opencv-python (all in project requirements).
Every exception is caught and logged; caller always gets a valid path back.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import shutil
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

_N_CTRL_PTS  = 20   # DTW path sample count; more = tighter sync, more ffmpeg segments
_MAX_SPEED   = 3.0  # max stretch/squeeze factor per segment
_ANALYSIS_FPS = 8.0  # feature sample rate; 8fps gives sub-second resolution without heavy DTW


# ─────────────────────────────────────────────────────────────────────────────
# DTW warp computation
# ─────────────────────────────────────────────────────────────────────────────

def _compute_dtw_warp(video_path: str, audio_path: str) -> tuple[list, list]:
    """Compute piecewise-linear warp aligning video motion to audio energy.

    Returns (v_ctrl, a_ctrl): v_ctrl[i] seconds in the input video should
    appear at a_ctrl[i] seconds in the output.  Both lists include 0 and
    duration bookends and are strictly monotone.
    """
    import librosa
    import cv2
    from scipy.ndimage import gaussian_filter1d

    # -- Audio feature: onset strength + RMS ----------------------------------
    y, sr    = librosa.load(audio_path, sr=None, mono=True)
    aud_dur  = float(len(y)) / sr
    hop      = max(1, int(sr / _ANALYSIS_FPS))
    real_afps = sr / hop

    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    rms_env   = librosa.feature.rms(y=y, hop_length=hop)[0]
    n_a       = min(len(onset_env), len(rms_env))
    onset_env, rms_env = onset_env[:n_a], rms_env[:n_a]

    audio_feat  = (0.65 * onset_env / (onset_env.max() + 1e-8) +
                   0.35 * rms_env   / (rms_env.max()   + 1e-8))
    audio_times = np.arange(n_a) * hop / sr
    log.info("[beat-sync] audio: %.2fs, %d frames @ %.1ffps", aud_dur, n_a, real_afps)

    # -- Video feature: frame difference (motion energy) ----------------------
    cap      = cv2.VideoCapture(video_path)
    vid_fps  = cap.get(cv2.CAP_PROP_FPS) or 24.0
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    vid_dur  = float(n_frames) / vid_fps
    skip     = max(1, int(round(vid_fps / _ANALYSIS_FPS)))
    real_vfps = vid_fps / skip

    diffs, prev, idx = [], None, 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if idx % skip == 0:
            gray  = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            small = cv2.resize(gray, (160, 90))
            if prev is not None:
                diffs.append(float(np.mean(np.abs(
                    small.astype(np.float32) - prev.astype(np.float32)
                ))))
            prev = small
        idx += 1
    cap.release()

    if not diffs:
        raise RuntimeError("No video frames read")

    video_feat  = np.array(diffs, dtype=np.float32)
    video_feat /= (video_feat.max() + 1e-8)
    video_times = np.arange(len(video_feat)) * skip / vid_fps
    log.info("[beat-sync] video: %.2fs, %d frames @ %.1ffps", vid_dur, len(video_feat), real_vfps)

    # -- Smooth both signals (~0.6s window) then DTW --------------------------
    a_smooth = _smooth(audio_feat, real_afps)
    v_smooth = _smooth(video_feat, real_vfps)

    log.info("[beat-sync] DTW on (%d x %d) matrix ...", len(v_smooth), len(a_smooth))
    D, wp = librosa.sequence.dtw(
        X=v_smooth.reshape(1, -1),
        Y=a_smooth.reshape(1, -1),
        subseq=False, backtrack=True, global_constraints=False,
    )
    wp = wp[::-1]  # returned end->start; flip to start->end
    log.info("[beat-sync] DTW path: %d steps", len(wp))

    # -- Sample N control points from path ------------------------------------
    v_path = video_times[np.minimum(wp[:, 0], len(video_times) - 1)]
    a_path = audio_times[np.minimum(wp[:, 1], len(audio_times) - 1)]

    si = np.linspace(0, len(v_path) - 1, _N_CTRL_PTS, dtype=int)
    v_ctrl = [0.0] + list(v_path[si]) + [vid_dur]
    a_ctrl = [0.0] + list(a_path[si]) + [aud_dur]

    # Enforce strict monotonicity
    v_out, a_out = [v_ctrl[0]], [a_ctrl[0]]
    for i in range(1, len(v_ctrl)):
        if v_ctrl[i] > v_out[-1] + 0.02 and a_ctrl[i] > a_out[-1] + 0.02:
            v_out.append(v_ctrl[i])
            a_out.append(a_ctrl[i])
    if v_out[-1] < vid_dur - 0.05:
        v_out.append(vid_dur)
        a_out.append(aud_dur)

    log.info("[beat-sync] %d control points", len(v_out))
    return v_out, a_out


def _smooth(arr, fps):
    from scipy.ndimage import gaussian_filter1d
    return gaussian_filter1d(arr, sigma=max(1.0, fps * 0.6))


# ─────────────────────────────────────────────────────────────────────────────
# FFmpeg warp application
# ─────────────────────────────────────────────────────────────────────────────

def _apply_warp(input_video: str, v_ctrl: list, a_ctrl: list,
                output_path: str) -> bool:
    """Build and run ffmpeg setpts warp (video only, no audio).  Returns True on success."""
    n_segs       = len(v_ctrl) - 1
    filter_parts = []
    seg_labels   = []
    clamps       = 0

    for i in range(n_segs):
        vs, ve = v_ctrl[i], v_ctrl[i + 1]
        ao, ae = a_ctrl[i], a_ctrl[i + 1]
        orig   = ve - vs
        target = ae - ao
        if orig < 0.015 or target < 0.015:
            continue

        speed   = orig / target
        clamped = max(1.0 / _MAX_SPEED, min(_MAX_SPEED, speed))
        if abs(clamped - speed) > 0.02:
            clamps += 1
            target = orig / clamped

        pts_mult = target / orig
        label    = f"s{i}"
        filter_parts.append(
            f"[0:v]trim=start={vs:.6f}:end={ve:.6f},"
            f"setpts={pts_mult:.9f}*(PTS-STARTPTS)[{label}]"
        )
        seg_labels.append(f"[{label}]")

    if not seg_labels:
        log.warning("[beat-sync] no valid segments -- skipping warp")
        return False
    if clamps:
        log.info("[beat-sync] %d segments clamped to %.1fx", clamps, _MAX_SPEED)

    filter_parts.append(
        f"{''.join(seg_labels)}concat=n={len(seg_labels)}:v=1:a=0[outv]"
    )

    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", input_video,
             "-filter_complex", ";".join(filter_parts),
             "-map", "[outv]",
             "-c:v", "libx264", "-crf", "16", "-preset", "fast",
             "-pix_fmt", "yuv420p", "-an", output_path],
            capture_output=True, timeout=1200,
        )
        if r.returncode != 0:
            log.error("[beat-sync] warp encode failed: %s",
                      r.stderr.decode(errors="replace")[-600:])
            return False
        return True
    except Exception as e:
        log.error("[beat-sync] warp exception: %s", e)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def apply_beat_sync(merged_path: str, audio_path: str, job_dir: Path,
                    job_id: str, log_fn=None) -> str:
    """DTW-warp the video track so scene changes align with audio beat/onset peaks.

    Input:  merged_path -- the raw concat (no audio, just the unique video clips)
    Output: a warped video file (still no audio) whose motion peaks land on beats.
            The caller loops this to fill the song and then muxes audio.

    Returns the warped path on success, or merged_path unchanged on any failure.
    """
    def _log(msg):
        log.info(msg)
        if log_fn:
            log_fn(msg)

    _log("[beat-sync] starting DTW alignment pass ...")

    try:
        _check_deps()
    except ImportError as e:
        _log(f"[beat-sync] skipped -- missing dependency: {e}")
        return merged_path

    synced_path = str(job_dir / f"synced_{job_id[:6]}.mp4")

    try:
        v_ctrl, a_ctrl = _compute_dtw_warp(merged_path, audio_path)
    except Exception as e:
        _log(f"[beat-sync] DTW computation failed (skipping): {e}")
        log.exception("[beat-sync] DTW error")
        return merged_path

    try:
        ok = _apply_warp(merged_path, v_ctrl, a_ctrl, synced_path)
    except Exception as e:
        _log(f"[beat-sync] warp encode failed (skipping): {e}")
        log.exception("[beat-sync] warp error")
        return merged_path

    if not ok or not Path(synced_path).exists():
        _log("[beat-sync] output not produced -- keeping original")
        return merged_path

    _log(f"[beat-sync] done -> {Path(synced_path).name}")
    return synced_path


def _check_deps():
    import librosa   # noqa: F401
    import cv2       # noqa: F401
    import scipy     # noqa: F401
