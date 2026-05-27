"""Post-generation video retimer: warp video sections to align motion peaks to audio beats."""
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from core.ffmpeg_utils import probe_duration

log = logging.getLogger(__name__)

_MIN_SPEED = 0.4   # never slow a section below 0.4x (2.5x stretch)
_MAX_SPEED = 3.0   # never speed a section above 3x


def build_remap(video_dur: float, audio_dur: float, remap_points: list) -> list:
    """Convert sparse remap_points into a full piecewise segment list.

    remap_points: list of {video_t, target_t} -- "the event at video_t should
    land at target_t in the output."

    The final anchor is (video_dur, audio_dur) so the output is always exactly
    as long as the audio, regardless of intermediate speed changes.

    Returns list of {v_start, v_end, speed} covering [0, video_dur].
    speed > 1 = play faster; speed < 1 = play slower.
    """
    pts = sorted(remap_points, key=lambda p: p["video_t"])
    # Bookend: start at (0,0), end at (video_dur, audio_dur) so output = audio_dur
    anchors = [(0.0, 0.0)] + [(p["video_t"], p["target_t"]) for p in pts] + [(video_dur, audio_dur)]
    segments = []
    for i in range(len(anchors) - 1):
        v0, t0 = anchors[i]
        v1, t1 = anchors[i + 1]
        v_span = v1 - v0
        t_span = t1 - t0
        if v_span <= 0.01 or t_span <= 0.01:
            log.warning("[retimer] Skipping degenerate segment anchors[%d->%d]: "
                        "v_span=%.4f t_span=%.4f -- sync points too close together",
                        i, i + 1, v_span, t_span)
            continue
        # speed > 1: more video than time → play faster
        # speed < 1: less video than time → play slower
        speed = max(_MIN_SPEED, min(_MAX_SPEED, v_span / t_span))
        segments.append({"v_start": v0, "v_end": v1, "speed": speed})
    return segments


def retime_video(video_path: str, audio_path: str, out_path: str,
                 remap_points=None,
                 auto_align: bool = True):
    """Stretch/compress video sections to align motion peaks to audio beats.

    Returns (success, error_message).
    """
    from features.beat_sync.analyzer import analyze_audio_beats, analyze_video_motion

    video_dur = probe_duration(video_path)
    if not video_dur:
        return False, "Could not probe video duration"

    audio_dur = probe_duration(audio_path) or video_dur

    if remap_points is None and auto_align:
        audio_data = analyze_audio_beats(audio_path)
        video_data = analyze_video_motion(video_path)
        remap_points = _auto_remap(
            video_data.get("motion_peaks", []),
            audio_data.get("energy_peaks", []),
            audio_data.get("beat_times", []),
            video_dur,
            audio_dur,
            clip_boundaries=video_data.get("clip_boundaries", []),
        )

    if not remap_points:
        log.info("[retimer] No remap points -- muxing audio onto video as-is")
        return _mux_audio(video_path, audio_path, out_path, audio_dur)

    segments = build_remap(video_dur, audio_dur, remap_points)
    if not segments:
        return _mux_audio(video_path, audio_path, out_path, audio_dur)

    log.info("[retimer] Applying %d segments to %.1fs video -> %.1fs output",
             len(segments), video_dur, audio_dur)

    with tempfile.TemporaryDirectory() as tmp:
        # Only interpolate to 60fps when at least one segment needs slow-motion
        # (speed < 1.0). For pure speed-up jobs the interpolated frames are
        # discarded by setpts anyway, so the expensive minterpolate pass adds
        # nothing and can time out for long videos on CPU.
        needs_interp = any(seg["speed"] < 1.0 for seg in segments)
        interp_path = video_path
        if needs_interp:
            _interp_tmp = os.path.join(tmp, "interp.mp4")
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", video_path,
                 "-vf", "minterpolate=fps=60:mi_mode=mci",
                 "-an", "-c:v", "libx264", "-crf", "15", "-preset", "fast",
                 _interp_tmp],
                capture_output=True, timeout=600,
            )
            if r.returncode == 0 and Path(_interp_tmp).exists():
                interp_path = _interp_tmp
            else:
                log.warning("[retimer] minterpolate failed -- using original video")

        seg_files = []
        for j, seg in enumerate(segments):
            seg_out = os.path.join(tmp, f"seg_{j:03d}.mp4")
            speed = seg["speed"]
            # (PTS-STARTPTS) normalises each segment to start at zero before
            # speed scaling. Without STARTPTS, trimmed segments carry their
            # original large timestamp into the concat and create a gap.
            pts_expr = f"(PTS-STARTPTS)*{1.0/speed:.8f}"
            r2 = subprocess.run(
                ["ffmpeg", "-y", "-i", interp_path,
                 "-ss", f"{seg['v_start']:.4f}", "-to", f"{seg['v_end']:.4f}",
                 "-vf", f"setpts={pts_expr}",
                 "-an", "-c:v", "libx264", "-crf", "15", "-preset", "fast",
                 seg_out],
                capture_output=True, timeout=180,
            )
            if r2.returncode == 0 and Path(seg_out).exists():
                seg_files.append(seg_out)
            else:
                log.warning("[retimer] Segment %d failed: %s", j,
                            r2.stderr.decode(errors="replace")[-200:])

        if not seg_files:
            return _mux_audio(video_path, audio_path, out_path, audio_dur)

        # Concat segments into a single video track
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
            log.warning("[retimer] Concat failed: %s",
                        r3.stderr.decode(errors="replace")[-200:])
            return _mux_audio(video_path, audio_path, out_path, audio_dur)

        return _mux_audio(concat_path, audio_path, out_path, audio_dur)


def _auto_remap(motion_peaks: list, energy_peaks: list, beat_times: list,
                video_dur: float, audio_dur: float,
                snap_window: float = 2.5,
                clip_boundaries: list | None = None) -> list:
    """Match each video motion/cut point to the nearest audio beat or energy peak."""
    targets = [t for t in (motion_peaks or []) if 0 < t < video_dur]
    if not targets and clip_boundaries:
        targets = [t for t in clip_boundaries if 0.5 < t < video_dur - 0.5]
        log.info("[retimer] Using %d clip boundaries as alignment targets", len(targets))

    # Dense candidates: energy peaks + every beat (not just downbeats)
    candidates = sorted(set(list(energy_peaks) + list(beat_times)))
    if not candidates:
        log.warning("[retimer] No audio candidates")
        return []
    if not targets:
        log.warning("[retimer] No video targets")
        return []

    remap = []
    used = set()
    for vt in sorted(targets):
        best = min(candidates, key=lambda c: abs(c - vt))
        dist = abs(best - vt)
        if dist <= snap_window and best not in used and 0 < best < audio_dur:
            remap.append({"video_t": vt, "target_t": best})
            used.add(best)

    log.info("[retimer] Auto-remap: %d/%d targets snapped (window=%.1fs)",
             len(remap), len(targets), snap_window)
    return remap


def _mux_audio(video_path: str, audio_path: str, out_path: str, audio_dur: float = 0.0):
    """Mux audio onto video. Output is trimmed/padded to audio duration."""
    # Use -t audio_dur (not -shortest) so video never gets cut short.
    # If video is shorter than audio, ffmpeg pads with the last frame.
    t_args = ["-t", f"{audio_dur:.3f}"] if audio_dur > 0 else []
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-i", audio_path,
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]
        + t_args + [out_path],
        capture_output=True, timeout=600,
    )
    if r.returncode == 0 and Path(out_path).exists():
        return True, ""
    return False, r.stderr.decode(errors="replace")[-600:]
