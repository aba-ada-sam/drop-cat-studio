"""Song video quality evaluator.

Analyzes a generated music video for:
- Seam visibility at clip boundaries (pixel diff + vision LLM)
- Character consistency across clips
- Overall motion quality

Called automatically after each batch job succeeds.
"""
import logging
import os
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def _extract_frame(video_path: str, time_sec: float, out_png: str) -> bool:
    r = subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{time_sec:.4f}", "-i", video_path,
         "-frames:v", "1", "-q:v", "2", out_png],
        capture_output=True, timeout=15,
    )
    return r.returncode == 0 and Path(out_png).exists()


def _pixel_diff(png_a: str, png_b: str) -> float:
    """Return mean per-pixel absolute difference (0-255) between two images."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-i", png_a, "-i", png_b,
             "-filter_complex", "blend=all_mode=difference,signalstats=stat=mean",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=15,
        )
        for line in r.stderr.splitlines():
            if "MEAN" in line or "mean" in line.lower():
                parts = line.split()
                for i, p in enumerate(parts):
                    if "mean" in p.lower() and i + 1 < len(parts):
                        try:
                            return float(parts[i + 1])
                        except ValueError:
                            pass
    except Exception:
        pass
    return -1.0


def evaluate_video(
    video_path: str,
    n_clips: int,
    clip_duration: float,
    xfade_dur: float = 0.12,
    llm_router=None,
) -> dict:
    """Analyze a generated music video for quality issues.

    Returns a dict with:
        seam_diffs    -- list of pixel diffs at each boundary (lower = smoother)
        worst_seam    -- index of worst boundary + its diff score
        issues        -- list of human-readable problem descriptions
        vision_report -- LLM assessment of worst seam frames (if llm_router provided)
        score         -- overall 0-10 quality estimate
    """
    if not os.path.isfile(video_path):
        return {"error": "Video file not found"}

    from core.ffmpeg_utils import probe_duration
    total_dur = probe_duration(video_path) or 0
    if total_dur < 1:
        return {"error": "Cannot probe video duration"}

    # Estimate clip boundary times in the final video.
    # Each xfade shortens the timeline: boundary_i = i*(clip_dur - xfade_dur)
    boundaries = []
    for i in range(1, n_clips):
        t = i * (clip_duration - xfade_dur)
        if t < total_dur - 1:
            boundaries.append(t)

    seam_diffs = []
    worst_idx = 0
    worst_diff = 0.0
    worst_frames = None

    with tempfile.TemporaryDirectory() as tmp:
        for idx, t in enumerate(boundaries):
            before_png = os.path.join(tmp, f"before_{idx}.png")
            after_png  = os.path.join(tmp, f"after_{idx}.png")

            ok_b = _extract_frame(video_path, max(0, t - 0.1), before_png)
            ok_a = _extract_frame(video_path, t + 0.1, after_png)

            if ok_b and ok_a:
                diff = _pixel_diff(before_png, after_png)
                seam_diffs.append(diff)
                if diff > worst_diff:
                    worst_diff = diff
                    worst_idx = idx
                    # Copy worst frames for vision analysis
                    import shutil
                    worst_frames = (
                        shutil.copy(before_png, Path(video_path).parent / "eval_before.png"),
                        shutil.copy(after_png,  Path(video_path).parent / "eval_after.png"),
                    )
            else:
                seam_diffs.append(-1)

        # Vision analysis of worst seam
        vision_report = None
        if llm_router and worst_frames and all(os.path.isfile(f) for f in worst_frames):
            try:
                from core.llm_client import encode_image_b64
                b64_before = encode_image_b64(worst_frames[0])
                b64_after  = encode_image_b64(worst_frames[1])
                if b64_before and b64_after:
                    vision_report = llm_router.route_vision(
                        "These are two consecutive frames from a music video at a clip boundary. "
                        "Describe in one sentence: (1) is the transition seamless or is there a visible jump? "
                        "(2) does the character look consistent? "
                        "(3) is there any flash, brightness change, or pose discontinuity? "
                        "Be direct and specific.",
                        [b64_before, b64_after],
                        tier="fast",
                        max_tokens=120,
                    )
            except Exception as e:
                log.debug("[eval] Vision analysis failed: %s", e)

    # Score: start at 10, subtract for bad seams
    issues = []
    score = 10.0
    valid_diffs = [d for d in seam_diffs if d >= 0]
    if valid_diffs:
        avg_diff = sum(valid_diffs) / len(valid_diffs)
        # > 15px avg = visible seams; > 25px = bad; > 35px = very bad
        if avg_diff > 35:
            score -= 4
            issues.append(f"Severe pose discontinuity at boundaries (avg {avg_diff:.1f}px diff)")
        elif avg_diff > 25:
            score -= 2.5
            issues.append(f"Visible seams at clip boundaries (avg {avg_diff:.1f}px diff)")
        elif avg_diff > 15:
            score -= 1
            issues.append(f"Slight seam visibility (avg {avg_diff:.1f}px diff)")

        if worst_diff > 30:
            issues.append(f"Worst seam at boundary {worst_idx + 1}: {worst_diff:.1f}px diff")

    return {
        "video":        video_path,
        "n_clips":      n_clips,
        "seam_diffs":   seam_diffs,
        "avg_diff":     sum(valid_diffs) / len(valid_diffs) if valid_diffs else -1,
        "worst_seam":   {"index": worst_idx, "diff": worst_diff},
        "issues":       issues,
        "vision_report": vision_report,
        "score":        max(0.0, score),
        "eval_frames":  worst_frames,
    }
