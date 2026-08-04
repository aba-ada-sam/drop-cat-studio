"""Frame-grid arithmetic for audio-conditioned sing clips.

Ported into DCS proper 2026-08-04 (Tier-2). Spec of record: LIPSYNC_LEDGER.md
CURRENT RECIPE + the 2026-08-04 assembly entries. Pure arithmetic -- no ffmpeg,
no GPU, no I/O -- so every rule here is testable without rendering anything
(tests/test_sing_grid.py).

This module exists because DCS had NONE of this math. Measured 2026-08-04:
  - No 8k+1 quantization anywhere. video_generator.py rounds to the nearest ODD
    frame count, which is a different convention entirely. Integer-second
    durations at 24fps land on 8k+1 by luck; the beat planner emits fractional
    durations (9.317s -> 223f) that do not.
  - No conditioning-grip cap. The only cap is max_sec 19 -> 455f, and the song
    pipeline clamps clip duration to 12s -> 289f. Every sing clip in DCS could
    legally render at 289 frames, well past the ~250f ceiling where audio
    conditioning stops gripping -- and that failure is SILENT (the clip renders,
    looks structurally fine, and simply does not lip-sync).
  - No reconciliation between where a clip's conditioning audio was cut from the
    song and where that clip lands in the assembled video.

THE THREE RULES, each with the failure that earned it:

1. 8k+1 FRAME COUNTS. WanGP's LTX-2 arch spec declares frames_steps=8,
   frames_minimum=17. Ask for anything else and you do not get what you asked
   for -- which is how a 233-frame render ended up in a 236-frame slot.

2. CONTENT BUDGET != RENDER CAP. A clip is rendered with headroom it loses
   before assembly (boundary trim, and a crossfade at each junction). Splitting
   on the RENDER cap while rendering headroom on top pushes the actual render
   past the grip ceiling. Split on the CONTENT budget so the render stays under.
   (Found on the site pipeline while fixing the drift bug; adopted here.)

3. HEADROOM THEN TRIM, not pad-with-clones, for anything rendered fresh.
   Cloning frames to fill a slot freezes the mouth mid-word. Render longer than
   the slot and cut down to an exact integer frame count instead. Padding is
   still correct for ASSEMBLY of already-rendered material where the tail frames
   are a pristine SE-anchored pose (that is what makes Studio's 3-frame holds
   invisible) -- but it is the wrong default for a fresh render.
"""
from __future__ import annotations

import math

# --- the recipe's fixed constants (LIPSYNC_LEDGER CURRENT RECIPE) ------------
SING_FPS = 24                 # WanGP _ARCH_SPECS ltx2_19B. NOT 25.
FRAMES_STEP = 8               # frames_steps: counts are 8k+1
FRAMES_MIN = 17               # frames_minimum

# Audio-conditioning GRIP ceiling. Measured 2026-08-04: 4/4 windows synced at
# 249f vs 1/21 at 465-473f -- same recipe, same box, same slices, only frame
# count changed. NOT a rendering limit (WanGP renders longer happily); the clip
# renders clean and silently stops lip-syncing.
SING_MAX_FRAMES = 249

# Boundary trim applied to every rendered clip before assembly.
HEAD_TRIM_S = 0.08            # LTX-2 startup-frame flash
TAIL_TRIM_S = 0.20            # LTX-2 baked-in fade-out
TRIM_OVERHEAD_S = HEAD_TRIM_S + TAIL_TRIM_S

# Crossfade consumed at each junction when clips are joined.
XFADE_S = 0.25

# What a clip may actually CONTAIN, once its render headroom is subtracted.
# Split on THIS, never on SING_MAX_FRAMES -- see rule 2 above.
SING_CONTENT_MAX_FRAMES = SING_MAX_FRAMES - math.ceil(
    (TRIM_OVERHEAD_S + XFADE_S) * SING_FPS)


def quantize_8k1(frames: float, minimum: int = FRAMES_MIN) -> int:
    """Nearest valid 8k+1 frame count (never below `minimum`)."""
    f = max(minimum, int(round(frames)))
    k = round((f - 1) / FRAMES_STEP)
    return max(minimum, FRAMES_STEP * k + 1)


def quantize_8k1_ceil(frames: float, minimum: int = FRAMES_MIN) -> int:
    """Round UP to a valid 8k+1 count. Use this for RENDER lengths.

    Nearest-rounding can land BELOW what a slot needs (a 9.9s slot plus 0.28s
    headroom is 244.3f; nearest-8k+1 is 241 = 10.04s, and 9.76s survives the
    trim against a 9.9s slot). The shortfall would have to be filled with cloned
    frames, freezing the mouth. Rounding up costs a few frames of GPU and
    guarantees the exact-fit is always a trim.
    """
    f = max(minimum, int(math.ceil(frames)))
    k = math.ceil((f - 1) / FRAMES_STEP)
    return max(minimum, FRAMES_STEP * k + 1)


def frames_for_slot(slot_s: float, *, with_junction: bool = False,
                    fps: float = SING_FPS) -> int:
    """Frames to REQUEST so that, after trimming, the clip fills `slot_s`.

    with_junction=True adds the crossfade a following clip will consume, so the
    clip still covers its slot after the junction eats into it.
    """
    need = slot_s + TRIM_OVERHEAD_S + (XFADE_S if with_junction else 0.0)
    return quantize_8k1_ceil(need * fps)


def exceeds_grip_ceiling(num_frames: int) -> bool:
    """True if a render at this length is past the point conditioning grips."""
    return num_frames > SING_MAX_FRAMES


def split_content_windows(clip_dur: float, fps: float = SING_FPS,
                          max_content_frames: int = SING_CONTENT_MAX_FRAMES
                          ) -> list[tuple[float, float]]:
    """Split a clip into sub-windows whose RENDERS stay under the grip ceiling.

    Returns [(start_s, end_s), ...] relative to the clip's own start, always
    summing to exactly clip_dur (never leaving an uncovered tail -- a gap off
    the end is a silent hole in the song). A clip that already fits comes back
    as a single unchanged window, which is the common case.
    """
    total = quantize_8k1(clip_dur * fps)
    if total <= max_content_frames:
        return [(0.0, clip_dur)]

    n_parts = math.ceil(total / max_content_frames)
    part_frames = quantize_8k1(total / n_parts)
    # Quantization can round the parts down far enough that n of them no longer
    # cover the whole clip. Grow the count from the ACTUAL per-part size rather
    # than trusting the first estimate.
    while part_frames * n_parts < total:
        n_parts += 1
    part_dur = part_frames / fps

    out: list[tuple[float, float]] = []
    t = 0.0
    for i in range(n_parts):
        t1 = clip_dur if i == n_parts - 1 else min(clip_dur, t + part_dur)
        out.append((t, t1))
        t = t1
    return out


def assembled_start_times(clip_lengths_s: list[float],
                          xfade_s: float = XFADE_S) -> list[float]:
    """Where each clip ACTUALLY starts in the assembled video.

    Mirrors the crossfade concat's offset math: every junction pulls the
    timeline back by one fade. This is the function to compare against the
    positions the conditioning slices were cut from -- if the two disagree, the
    mouth is singing the wrong part of the song, and no per-clip metric can see
    it (each clip is individually perfect).
    """
    starts, cursor = [], 0.0
    for d in clip_lengths_s:
        starts.append(cursor)
        cursor += d - xfade_s
    return starts


def timeline_drift(planned_starts_s: list[float],
                   actual_clip_lengths_s: list[float],
                   xfade_s: float = XFADE_S) -> list[float]:
    """Per-clip (actual_start - planned_start). Positive = plays late.

    A non-zero, growing series here IS the assembly bug: takes that each scored
    synced, desynced by where they were placed. Studio shipped a cut with ~0.3s
    of this and it read as broken lip-sync; the site pipeline had 0.28s per clip
    compounding to 5.3s at its ceiling.
    """
    actual = assembled_start_times(actual_clip_lengths_s, xfade_s)
    n = min(len(planned_starts_s), len(actual))
    return [actual[i] - planned_starts_s[i] for i in range(n)]


def exact_slot_frames(slot_s: float, fps: float = SING_FPS) -> int:
    """The integer frame count a slot occupies. Assembly must hit this exactly.

    Never rely on ffmpeg's -t to make a clip fit: -t TRIMS a long clip and does
    nothing at all to a short one, so a short segment silently shrinks the
    timeline and everything after it plays early.
    """
    return max(1, int(round(slot_s * fps)))
