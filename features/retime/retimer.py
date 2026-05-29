"""Manual video retimer: warp a video's timeline to user-placed lock points.

Fully manual -- the caller supplies anchor pairs {src_t, dst_t} mapping source
video moments to output moments. There is NO automatic beat/peak detection
(that was the removed beat-sync feature). Between anchors the video is stretched
or compressed linearly via ffmpeg setpts; the original audio is re-muxed
unchanged so the video locks to the audio, not the other way around.
"""
import logging
import os

from core.ffmpeg_utils import probe_duration, run_ffmpeg

log = logging.getLogger(__name__)

# Clamp per-segment speed so a stray drag can't produce an extreme warp.
_MIN_FACTOR = 0.25   # fastest: source plays 4x speed
_MAX_FACTOR = 4.0    # slowest: source stretched 4x


def _build_points(anchors, total):
    """Sorted, strictly-increasing (src, dst) points with implicit endpoints.

    Always starts at (0, 0) and ends at (total, total) so the output is the same
    length as the source/audio -- interior anchors only redistribute the middle.
    """
    pts = [(0.0, 0.0)]
    for a in sorted(anchors, key=lambda x: float(x.get("src_t", 0))):
        s = max(0.0, min(total, float(a.get("src_t", 0))))
        d = max(0.0, min(total, float(a.get("dst_t", 0))))
        # Strictly increasing in both axes with a small minimum gap.
        if s > pts[-1][0] + 0.05 and d > pts[-1][1] + 0.05 and s < total - 0.05 and d < total - 0.05:
            pts.append((s, d))
    pts.append((total, total))
    return pts


def retime_video(job, in_path: str, out_path: str, anchors: list[dict]) -> str:
    """Warp in_path's video timeline to the anchors, re-muxing original audio.

    Returns out_path on success; raises on failure.
    """
    total = probe_duration(in_path) or 0.0
    if total <= 0:
        raise RuntimeError("Could not determine source video duration")

    pts = _build_points(anchors, total)
    job.update(progress=15, message=f"Retiming across {len(pts) - 1} segment(s)...")

    # Build one filter_complex: trim each source segment, setpts to its target
    # span, then concat the warped segments. Audio is mapped straight from the
    # source (0:a) and left untouched.
    chains = []
    labels = []
    for k in range(len(pts) - 1):
        s0, d0 = pts[k]
        s1, d1 = pts[k + 1]
        s_span = max(0.001, s1 - s0)
        d_span = max(0.001, d1 - d0)
        factor = max(_MIN_FACTOR, min(_MAX_FACTOR, d_span / s_span))
        chains.append(
            f"[0:v]trim={s0:.3f}:{s1:.3f},setpts=(PTS-STARTPTS)*{factor:.5f}[v{k}]"
        )
        labels.append(f"[v{k}]")
    concat = f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[vout]"
    filter_complex = ";".join(chains + [concat])

    has_audio = _probe_has_audio(in_path)
    cmd = ["ffmpeg", "-y", "-i", in_path, "-filter_complex", filter_complex,
           "-map", "[vout]"]
    if has_audio:
        cmd += ["-map", "0:a"]
    cmd += ["-t", f"{total:.3f}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "16", "-pix_fmt", "yuv420p"]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += ["-movflags", "+faststart", out_path]

    job.update(progress=35, message="Rendering retimed video...")
    r = run_ffmpeg(cmd, timeout=1800)
    if r.returncode != 0 or not os.path.isfile(out_path):
        err = (r.stderr.decode(errors="replace") if r.stderr else "")[-1500:]
        log.error("[retime] ffmpeg failed:\n%s", err)
        raise RuntimeError("Retime render failed -- see server log")

    job.update(progress=90, message="Finalizing...")
    return out_path


def _probe_has_audio(path: str) -> bool:
    try:
        r = run_ffmpeg(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            timeout=30,
        )
        return bool((r.stdout or b"").strip())
    except Exception:
        return False
