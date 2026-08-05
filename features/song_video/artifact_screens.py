"""Artifact SCREENS for LTX-2 sing renders -- ribbons, red strands, yellow glyphs.

Ported into DCS proper 2026-08-04 (Tier-2) from the session tooling that earned
these thresholds the hard way. Spec of record: LIPSYNC_LEDGER.md, the
2026-08-04 section. Everything here is CPU: ffmpeg pipes + numpy, no GPU, no
model, no worker -- so it can run in tests and in batch triage.

WHY "SCREENS" AND NOT "GATES" -- read this before wiring anything to an
auto-reject:
  A screen RANKS and SURFACES suspect frames for a human to look at. A gate
  REJECTS work on its own. This module deliberately ships as screens, because
  the day these detectors were built produced three separate proofs that a
  detector confident enough to reject is a detector that will silently throw
  away good work:
    - the round-4 bright-streak metric scored the GOLDEN reference (AWM00001)
      worse than the humanly-rejected cut it was condemning, and rejected
      eye-verified-clean takes at a ~65% rate. Retracted the same day.
    - the transient-white detector (4x downsample + static mask) called a
      visibly ribbon-infested file "below baseline" -- downsampling erases the
      1-3px strands that ARE the artifact.
    - the red metric's max on an eye-clean take (final_07_0) EXCEEDED its own
      known-bad reference. Confirmed false positive, still unexplained.
  So: RIBBON has a validated numeric band and may be used as a gate (see
  ribbon_verdict). RED and YELLOW rank frames for the eye and never reject on
  their own. The ledger rule is "no validation, no gating" -- if you add a
  metric here, validate it against a known-good AND a known-bad first, and
  write the references into its docstring the way these are.

The other half of the discipline is EVIDENCE HYGIENE: worst_frames() writes into
a directory that must be born empty (see dump_worst_frames). Leftover frames
from a previous run cost 20 minutes of false DEFECTIVE verdicts on 2026-08-04,
because run 1's dumps sat beside run 2's under the same filenames.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import numpy as np

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

# ---- ribbon (thin near-white strands that REDRAW every frame) ----------------
# Metric: full-res bright mask min(R,G,B) > RIBBON_BRIGHT, XOR'd between
# consecutive frames, as a percentage of pixels that flipped.
# Physical rationale: legit bright content (windows, paper, denim highlights) is
# static or moves coherently with the subject, so it shifts a few px/frame and
# flips few pixels. Ribbons are redrawn from scratch every frame and flip a lot.
RIBBON_BRIGHT = 190
# Eye-calibrated on FIXED4 (both ends, full res): infested p95 1.07, clean 0.24.
RIBBON_P95_INFESTED = 0.50    # >= this: infested
RIBBON_P95_CLEAN = 0.30       # <  this: clean; between the two: EYE-CHECK
RIBBON_MAX_INFESTED = 1.0

# ---- red strands (dark-red confetti/ribbon, invisible to the bright mask) ----
# min(R,G,B) > 190 can never fire on these: they have low G and B. Two detectors
# are needed; neither is a superset of the other.
# RED_SAT sits in a MEASURED gap: brown crates/wood run R-max(G,B) ~20-35, the
# artifact strands are brick red at >50. RED_FLOOR suppresses dark noise.
RED_SAT = 45
RED_FLOOR = 90

# NOT IMPLEMENTED HERE, deliberately named so nobody assumes otherwise: the
# CURRENT RECIPE's pre-ranking stack is ribbon + yellow-glyph (<=0.02% excess
# over the source image's own yellow) + transient-white (<=2.0%) + luma (mean
# within 25% of source, peak <1.6x). This module ships the two FLICKER screens
# only. Yellow needs the source image as a baseline and luma needs signalstats;
# both live in tools/regen_offender_clips.py today. Do not read screen_window()
# as "the gate stack" -- it is two of four.


def _probe_size(path: str) -> tuple[int, int] | None:
    r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x",
                        str(path)], capture_output=True, timeout=60)
    m = re.search(r"(\d+)x(\d+)", r.stdout.decode(errors="replace"))
    return (int(m.group(1)), int(m.group(2))) if m else None


def frame_iter(path: str, ss: float | None = None, t: float | None = None):
    """Yield (index, HxWx3 uint8 RGB) at FULL RESOLUTION.

    Full res is load-bearing, not laziness: the artifact this feeds is 1-3px
    wide and a 4x decimation erases it completely (measured -- a downsampled
    detector scored a visibly infested file "below baseline"). Do not add a
    scale filter here to make it faster.
    """
    size = _probe_size(path)
    if not size:
        return
    w, h = size
    cmd = [FFMPEG, "-v", "error"]
    if ss is not None:
        cmd += ["-ss", f"{ss:.4f}"]
    cmd += ["-i", str(path)]
    if t is not None:
        cmd += ["-t", f"{t:.4f}"]
    cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    n, size_bytes = 0, w * h * 3
    try:
        while True:
            buf = proc.stdout.read(size_bytes)
            if not buf or len(buf) < size_bytes:
                break
            yield n, np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3)
            n += 1
    finally:
        try:
            proc.stdout.close()
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            pass


def ribbon_series(path: str, ss: float | None = None, t: float | None = None,
                   thresh: int = RIBBON_BRIGHT) -> np.ndarray:
    """Per-consecutive-pair ribbon score (percent of bright pixels that flipped).

    Returns n-1 values for n decoded frames. INDEX i IS THE PAIR (i, i+1), so
    the frame that actually CHANGED is i+1 -- see pair_index_to_time(). Getting
    this wrong dumps the frame before the artifact and makes the evidence look
    clean.
    """
    prev, out = None, []
    for _, fr in frame_iter(path, ss, t):
        cur = fr.min(axis=2) > thresh
        if prev is not None:
            out.append(100.0 * np.logical_xor(cur, prev).mean())
        prev = cur
    return np.array(out, dtype=float)


def red_series(path: str, ss: float | None = None, t: float | None = None,
                sat: int = RED_SAT, floor: int = RED_FLOOR) -> np.ndarray:
    """Per-consecutive-pair dark-red-strand score. Same pair-index rule as above."""
    prev, out = None, []
    for _, fr in frame_iter(path, ss, t):
        # int16 BEFORE subtracting: on uint8 the R - max(G,B) subtraction wraps
        # around and the mask inverts, which reads as "everything is red".
        fi = fr.astype(np.int16)
        cur = ((fi[:, :, 0] - np.maximum(fi[:, :, 1], fi[:, :, 2])) > sat) & (fi[:, :, 0] > floor)
        if prev is not None:
            out.append(100.0 * np.logical_xor(cur, prev).mean())
        prev = cur
    return np.array(out, dtype=float)


def pair_index_to_time(i: int, window_start_s: float, fps: float = 24.0) -> float:
    """Timestamp of the CHANGED frame for pair index i (frames i and i+1)."""
    return window_start_s + (i + 1) / float(fps)


def summarize(series: np.ndarray) -> dict:
    if series.size == 0:
        # An empty decode must FAIL a gate, never pass it silently.
        return {"p50": 999.0, "p95": 999.0, "max": 999.0, "n": 0}
    return {"p50": float(np.percentile(series, 50)),
            "p95": float(np.percentile(series, 95)),
            "max": float(series.max()), "n": int(series.size)}


def ribbon_verdict(p95: float, mx: float) -> str:
    """The ONE numeric verdict in this module (validated both ends on FIXED4).

    Returns "infested" | "eye-check" | "clean". Note that "clean" here means
    "clean OF RIBBONS" -- it says nothing about red strands, yellow glyphs, or
    the carved/embossed dark text that no brightness detector can see at all.
    A window is only clean when a human has looked at it.
    """
    if p95 >= RIBBON_P95_INFESTED or mx >= RIBBON_MAX_INFESTED:
        return "infested"
    if p95 < RIBBON_P95_CLEAN:
        return "clean"
    return "eye-check"


def screen_window(path: str, ss: float | None = None, t: float | None = None) -> dict:
    """Run every screen over one window. Pure measurement -- decides nothing."""
    rib = ribbon_series(path, ss, t)
    red = red_series(path, ss, t)
    rs, ds = summarize(rib), summarize(red)
    return {
        "ribbon": rs,
        "red": ds,
        "ribbon_verdict": ribbon_verdict(rs["p95"], rs["max"]),
        # Deliberately no red verdict: an eye-clean take (final_07_0) once
        # scored a higher red max than the known-bad reference. Rank, don't rule.
        "red_verdict": None,
        "worst_ribbon_pairs": [int(i) for i in np.argsort(rib)[-3:][::-1]] if rib.size else [],
        "worst_red_pairs": [int(i) for i in np.argsort(red)[-2:][::-1]] if red.size else [],
    }


# Written into every dump dir this module creates. Its presence is what
# authorizes wiping the directory on a later run.
_DUMP_MARKER = ".artifact_screens_dump"


def _prepare_dump_dir(out_dir: str) -> Path:
    """Return an EMPTY dump dir, without ever deleting someone else's data.

    Dump dirs must be born empty -- leftover worst-frames from an earlier run
    sat beside a new run's and produced confident DEFECTIVE verdicts on windows
    that had already been replaced (2026-08-04, 20 minutes lost). But the
    obvious implementation, rmtree on a caller-supplied path, is a foot-gun: it
    recursively deletes whatever string it is handed. So a directory is only
    wiped if it carries this module's own marker file, i.e. we made it. A
    directory we did not create is required to be empty and is never deleted.
    """
    out = Path(out_dir)
    if out.exists():
        if not out.is_dir():
            raise NotADirectoryError(f"dump target exists and is not a directory: {out}")
        if (out / _DUMP_MARKER).is_file():
            shutil.rmtree(out, ignore_errors=True)
        elif any(out.iterdir()):
            raise FileExistsError(
                f"refusing to wipe {out}: it has contents but no {_DUMP_MARKER} "
                f"marker, so this module did not create it. Point at a fresh path "
                f"or empty it deliberately.")
    out.mkdir(parents=True, exist_ok=True)
    (out / _DUMP_MARKER).write_text("artifact_screens dump dir; safe to wipe\n",
                                    encoding="ascii")
    return out


def dump_worst_frames(path: str, out_dir: str, window_start_s: float,
                       screen: dict, fps: float = 24.0,
                       uniform_every_s: float = 1.0,
                       window_dur_s: float | None = None) -> list[str]:
    """Write the frames a human should actually look at. Returns paths written.

    THE DIRECTORY IS EMPTIED FIRST, ON PURPOSE. On 2026-08-04 leftover
    worst-frames from an earlier run sat beside a new run's dumps under the same
    filenames and produced confident DEFECTIVE verdicts on windows that had
    already been replaced -- 20 minutes lost to disambiguating by file mtime.
    Any evidence directory an inspector reads must be born empty.

    Uniform samples are included alongside the detector's picks because the
    detectors are blind to whole artifact classes: dark carved/embossed
    lettering is invisible to every brightness and color metric here, and was
    only ever found by looking at ordinary frames.
    """
    out = _prepare_dump_dir(out_dir)

    times: set[float] = set()
    for i in screen.get("worst_ribbon_pairs", []):
        times.add(round(pair_index_to_time(i, window_start_s, fps), 3))
    for i in screen.get("worst_red_pairs", []):
        times.add(round(pair_index_to_time(i, window_start_s, fps), 3))
    if window_dur_s:
        ts = 0.0
        while ts < window_dur_s - 0.02:      # epsilon: never grab past the end
            times.add(round(window_start_s + ts, 3))
            ts += uniform_every_s

    written = []
    for ts in sorted(times):
        dst = out / f"t{ts:08.3f}.png"
        r = subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", f"{ts:.4f}",
                            "-i", str(path), "-frames:v", "1", str(dst)],
                           capture_output=True, timeout=120)
        if r.returncode == 0 and dst.exists():
            written.append(str(dst))
    return written


def dump_seam_frames(path: str, out_dir: str, seam_frames: list[int],
                      fps: float = 24.0) -> list[str]:
    """Both frames either side of every splice seam -- assembly evidence.

    A seam that drops or duplicates a frame is invisible in any single window's
    numbers and only shows up as lip-sync drift later in the song.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for f in seam_frames:
        for fi in (f - 1, f):
            dst = out / f"seam_f{fi:05d}.png"
            r = subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", f"{fi / fps:.4f}",
                                "-i", str(path), "-frames:v", "1", str(dst)],
                               capture_output=True, timeout=120)
            if r.returncode == 0 and dst.exists():
                written.append(str(dst))
    return written
