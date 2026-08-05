"""_song_clip_start_times: the wiring of sing_grid's timeline math into the
song pipeline's conditioning-slice start times. NO GPU, NO ffmpeg, NO I/O --
pure arithmetic, same discipline as test_sing_grid.py.

Companion to test_sing_grid.py, which proves assembled_start_times /
timeline_drift are correct IN THE ABSTRACT (group C already shows the OLD
pipeline formula drifts 3.08s by clip 12). This file proves the WIRING: that
pipeline.py's actual _song_clip_start_times function feeds those proven
functions the right numbers -- specifically the edge-aware boundary trim
(clip 0 loses only the tail trim, the last clip only the head trim, everyone
between loses both) that sing_grid's own flat TRIM_OVERHEAD_S constant does
not know how to apply by itself.

THE INVARIANT (task spec, verbatim): for N clips of requested duration D,
slice_start[i] == assembled position of clip i's first frame after trims and
crossfades, exact to 1 frame at 24fps.

RUN IT WITH:  python tests/test_clip_start_times.py
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from features.song_video import sing_grid  # noqa: E402
from features.song_video.pipeline import _song_clip_start_times  # noqa: E402

PASS = 0
FAIL = 0


def ok(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + msg)
    else:
        FAIL += 1
        print("  FAIL " + msg)


def _true_length(idx: int, requested: float, n: int) -> float:
    """Ground truth post-trim length, reconstructed independently of pipeline.py
    (mirrors pipeline.py's own GPU-phase boundary trim, ~line 1656-1695, but
    written fresh here rather than imported -- if pipeline.py's real trim
    logic ever silently diverges from this, THAT is exactly the kind of
    drift this test exists to catch)."""
    if n <= 1:
        return requested
    if idx == 0:
        return requested - sing_grid.TAIL_TRIM_S
    if idx == n - 1:
        return requested - sing_grid.HEAD_TRIM_S
    return requested - sing_grid.TRIM_OVERHEAD_S


FPS = sing_grid.SING_FPS
ONE_FRAME_S = 1.0 / FPS

print(f"\n-- A: THE INVARIANT -- slice_start[i] == true assembled position, "
     f"exact to 1 frame ({ONE_FRAME_S * 1000:.2f}ms) at {FPS}fps, for N=1..10 "
     f"clips of D=6.0s --")
D, XFADE, PAD = 6.0, 0.12, 1.0
for N in range(1, 11):
    durations = [D] * N
    got = _song_clip_start_times(durations, PAD, xfade_s=XFADE)
    true_lengths = [_true_length(i, D, N) for i in range(N)]
    expected = [PAD + s for s in sing_grid.assembled_start_times(true_lengths, xfade_s=XFADE)]

    ok(len(got) == N, f"N={N}: returns {len(got)} start times for {N} clips")
    max_diff_s = max((abs(g - e) for g, e in zip(got, expected)), default=0.0)
    ok(max_diff_s < ONE_FRAME_S,
       f"N={N} D={D}s: worst slice_start error {max_diff_s * 1000:.4f}ms, "
       f"under 1 frame ({ONE_FRAME_S * 1000:.2f}ms)")
    # Frame-exact per clip via sing_grid.exact_slot_frames (the third named
    # function -- rounding both sides to integer frames must not disagree by
    # more than 1 frame, the literal acceptance bound in the task spec).
    frame_mismatches = [
        i for i in range(N)
        if abs(sing_grid.exact_slot_frames(got[i]) - sing_grid.exact_slot_frames(expected[i])) > 1
    ]
    ok(not frame_mismatches,
       f"N={N}: exact_slot_frames agrees on every clip's start (mismatches: {frame_mismatches})")

print("\n-- B: edge cases --")
ok(_song_clip_start_times([], 1.0) == [], "empty clip_durations -> empty output, no crash")
ok(_song_clip_start_times([6.0], 1.0, xfade_s=0.12) == [1.0],
   "a single clip loses NO boundary trim (nothing precedes or follows it) -- "
   "starts exactly at pad_before")
ok(_song_clip_start_times([9.0], 0.0, xfade_s=0.12) == [0.0],
   "single clip, pad_before=0 -> starts at exactly 0.0")

print("\n-- C: this is a REAL fix, not a no-op -- OLD formula vs NEW, same "
     "N=12/D=7.0 scenario as test_sing_grid.py group C --")
N, CLIP, PAD2, SONG_XFADE = 12, 7.0, 1.0, 0.12


def _old_formula(durations, pad_before, xfade_s):
    """The exact pre-fix formula (preserved here so this comparison stays
    meaningful even after the live code has moved on): T_i on the REQUESTED
    duration, corrected only for the crossfade -- never for boundary trim."""
    start_t = pad_before
    out = []
    for idx, d in enumerate(durations):
        out.append(max(0.0, start_t - idx * xfade_s))
        start_t += d
    return out


old_starts = _old_formula([CLIP] * N, PAD2, SONG_XFADE)
new_starts = _song_clip_start_times([CLIP] * N, PAD2, xfade_s=SONG_XFADE)
true_lengths12 = [_true_length(i, CLIP, N) for i in range(N)]
true_starts = [PAD2 + s for s in sing_grid.assembled_start_times(true_lengths12, xfade_s=SONG_XFADE)]

old_drift = [o - t for o, t in zip(old_starts, true_starts)]
new_drift = [n_ - t for n_, t in zip(new_starts, true_starts)]

ok(abs(old_drift[-1]) > 1.0,
   f"OLD formula: clip {N - 1} was {old_drift[-1]:+.2f}s off its true assembled "
   f"position -- reproduces the live bug ROLLBACK_MAP finding (b) describes")
ok(all(abs(old_drift[i]) <= abs(old_drift[i + 1]) + 1e-9 for i in range(N - 1)),
   "OLD formula's error is cumulative (grows every clip), matching "
   "test_sing_grid.py group C's characterization")
ok(max(abs(d) for d in new_drift) < ONE_FRAME_S,
   f"NEW _song_clip_start_times: worst drift {max(abs(d) for d in new_drift) * 1000:.3f}ms, "
   f"under one frame at every one of {N} clips -- the fix actually closes the gap")

print("\n-- D: BONUS -- the invariant also holds for NON-uniform clip durations "
     "and non-trivial pad_before (the task's literal spec is uniform D; the fix "
     "must not be silently special-cased to that one shape) --")
random.seed(20260805)
worst_diff = 0.0
for _trial in range(30):
    N = random.randint(1, 12)
    durations = [round(random.uniform(4.0, 12.0), 3) for _ in range(N)]
    pad = round(random.uniform(0.0, 3.0), 3)
    got = _song_clip_start_times(durations, pad, xfade_s=0.12)
    true_lengths = [_true_length(i, d, N) for i, d in enumerate(durations)]
    expected = [pad + s for s in sing_grid.assembled_start_times(true_lengths, xfade_s=0.12)]
    diff = max((abs(g - e) for g, e in zip(got, expected)), default=0.0)
    worst_diff = max(worst_diff, diff)
    if diff >= 1e-9:
        ok(False, f"trial {_trial}: N={N} durations={durations} pad={pad}: diff {diff:.2e}s")
ok(worst_diff < 1e-9,
   f"30 randomized trials (N=1..12, D in [4,12], pad in [0,3]): worst diff "
   f"{worst_diff:.2e}s -- floating-point exact, not merely under a frame")

print("\n" + (f"ALL {PASS} PASS" if FAIL == 0 else f"{FAIL} FAIL / {PASS + FAIL} total"))
sys.exit(0 if FAIL == 0 else 1)
