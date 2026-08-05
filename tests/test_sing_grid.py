"""Frame-grid arithmetic tests. NO GPU, NO ffmpeg, NO renders -- pure math.

Two jobs:
  1. Prove the ported rules hold (8k+1, grip ceiling, headroom-then-trim,
     exact slots).
  2. REPRODUCE THE BUGS THAT ARE LIVE IN DCS RIGHT NOW as failing-before
     assertions, so the fix has a witness and a regression has a tripwire.
     Group C is the one that matters: DCS's song pipeline today lays out
     conditioning-slice positions on one timeline and assembles clips on a
     different one, and nothing in the repo compares them.

RUN IT WITH:  python tests/test_sing_grid.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from features.song_video.sing_grid import (  # noqa: E402
    FRAMES_MIN, SING_CONTENT_MAX_FRAMES, SING_FPS, SING_MAX_FRAMES,
    TRIM_OVERHEAD_S, XFADE_S, assembled_start_times, exact_slot_frames,
    exceeds_grip_ceiling, frames_for_slot, quantize_8k1, quantize_8k1_ceil,
    split_content_windows, timeline_drift,
)

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


print("\n-- A: 8k+1 quantization is the real convention (DCS uses odd-rounding) --")
for f in [17, 25, 100, 200, 249, 300]:
    q = quantize_8k1(f)
    ok((q - 1) % 8 == 0 and q >= FRAMES_MIN, f"quantize_8k1({f}) = {q} is 8k+1")
ok(quantize_8k1(1) == FRAMES_MIN, f"below the floor clamps to {FRAMES_MIN}")
ok(quantize_8k1_ceil(242) >= 242 and (quantize_8k1_ceil(242) - 1) % 8 == 0,
   f"ceil variant rounds UP to {quantize_8k1_ceil(242)}, never below the ask")
# DCS's actual current math, for contrast: max(17, int(dur*fps)) then force odd.
dcs_frames = lambda d: (lambda n: n + 1 if n % 2 == 0 else n)(max(17, int(d * 24)))
ok((dcs_frames(9.317) - 1) % 8 != 0,
   f"DCS's odd-rounding gives {dcs_frames(9.317)}f for a 9.317s beat-planned "
   f"clip -- NOT 8k+1 (this is the live gap)")

print("\n-- B: the grip ceiling is respected once headroom is counted --")
ok(SING_CONTENT_MAX_FRAMES < SING_MAX_FRAMES,
   f"content budget {SING_CONTENT_MAX_FRAMES}f sits below the {SING_MAX_FRAMES}f "
   f"render cap, by exactly the headroom a clip loses")
for clip_dur in [6.0, 9.9, 12.0, 19.7, 30.0]:
    wins = split_content_windows(clip_dur)
    total = sum(b - a for a, b in wins)
    ok(abs(total - clip_dur) < 1e-6,
       f"{clip_dur}s -> {len(wins)} window(s) covering exactly {total:.4f}s (no gap)")
    worst = max(frames_for_slot(b - a, with_junction=(j < len(wins) - 1))
                for j, (a, b) in enumerate(wins))
    ok(not exceeds_grip_ceiling(worst),
       f"{clip_dur}s: largest RENDER {worst}f stays under the {SING_MAX_FRAMES}f ceiling")

# THE TRAP, stated as a test: splitting on the render cap instead of the content
# budget produces renders at or past the ceiling. 20.75s is chosen because it
# splits into two full-budget parts and breaches outright; 19.7s is the subtler
# case that lands EXACTLY on 249 -- at the ceiling with zero margin, which is
# how a "safe" split silently becomes an unsyncable render after any later
# change to the trim or fade constants.
# THE TRAP, shown as arithmetic rather than by calling the function -- the
# function now SOLVES for the render constraint, so it can no longer be talked
# into breaching by passing a bigger budget (that hardening is the point). What
# the trap costs is still worth pinning: sizing windows against the render cap
# while rendering headroom on top puts the render past the ceiling.
import math as _math  # noqa: E402
for dur, breaches in ((20.75, True), (19.7, False)):
    n_naive = max(1, _math.ceil(dur * SING_FPS / SING_MAX_FRAMES))
    naive_render = frames_for_slot(dur / n_naive, with_junction=True)
    how = ("PAST the ceiling (silent no-sync)" if breaches
           else "exactly AT the ceiling, zero margin: the subtler half of the trap")
    ok((naive_render > SING_MAX_FRAMES) == breaches,
       f"{dur}s sized against the {SING_MAX_FRAMES}f RENDER cap renders "
       f"{naive_render}f -- {how}")
    safe = split_content_windows(dur)
    safe_worst = max(frames_for_slot(b - a, with_junction=True) for a, b in safe)
    ok(safe_worst <= SING_MAX_FRAMES,
       f"{dur}s split by the solver renders {safe_worst}f, at or under the "
       f"{SING_MAX_FRAMES}f ceiling (249 is itself the proven-good count)")

print("\n-- B2: EXHAUSTIVE invariants (red team found real overflows here) --")
# Every one of these was a live defect found by adversarial sweep, not theory:
#   2101f/5829f/9557f -> a 237-frame last window against a 236-frame budget,
#     rendering 257f, 8 past the grip ceiling (quantize_8k1 rounds DOWN, and
#     sizing the parts against the rounded value dumps the remainder on the
#     last window).
#   1005f and 79 other durations -> a ZERO-LENGTH trailing window, which
#     downstream becomes a 17-frame GPU render of nothing.
bad_budget, bad_ceiling, bad_empty, bad_sum = [], [], [], []
for nf in range(17, 12001):
    d = nf / SING_FPS
    wins = split_content_windows(d)
    if not wins or any(b - a <= 0 for a, b in wins):
        bad_empty.append(nf)
        continue
    if abs(sum(b - a for a, b in wins) - d) > 1e-6:
        bad_sum.append(nf)
    for j, (a, b) in enumerate(wins):
        # THE INVARIANT THAT MATTERS is the RENDER one. The content budget is
        # guidance for sizing; a window may quantize a step above it and still
        # render at exactly 249, which is the proven-good count make_clip.py
        # itself uses. Asserting the budget exactly over-constrains at the
        # rounding boundary (236.5 frames sits precisely between 233 and 241)
        # and would fail on windows that are demonstrably fine.
        # with_junction on EVERY window, not just interior ones: the last
        # sub-window of one clip abuts the next clip, so it pays a junction too.
        if frames_for_slot(b - a, with_junction=True) > SING_MAX_FRAMES:
            bad_ceiling.append(nf)
        if quantize_8k1((b - a) * SING_FPS) > SING_MAX_FRAMES:
            bad_budget.append(nf)
ok(not bad_empty, f"no zero-length/empty windows over 17..12000f "
                  f"(was {len(bad_empty)} cases; e.g. {bad_empty[:3]})")
ok(not bad_sum, f"windows always sum to the input ({len(bad_sum)} failures)")
ok(not bad_budget, f"no window's own content exceeds the {SING_MAX_FRAMES}f "
                   f"ceiling (was e.g. {bad_budget[:3]})")
ok(not bad_ceiling, f"no RENDER exceeds the {SING_MAX_FRAMES}f grip ceiling even "
                    f"with a junction on every window (was e.g. {bad_ceiling[:3]})")

print("\n-- C: DCS's LIVE drift bug, reproduced as arithmetic --")
# Measured from the code: prep lays out conditioning-slice start times assuming
# each clip advances the timeline by (planned_duration - 0.12); each clip then
# actually advances by (duration - 0.28 boundary trim) and loses the xfade too.
N, CLIP = 12, 7.0
SONG_XFADE = 0.12
planned = [i * (CLIP - SONG_XFADE) for i in range(N)]
live_lengths = [CLIP - TRIM_OVERHEAD_S] * N          # what DCS assembles today
drift_live = timeline_drift(planned, live_lengths, xfade_s=SONG_XFADE)
ok(abs(drift_live[-1]) > 1.0,
   f"DCS today: clip {N - 1} lands {drift_live[-1]:+.2f}s off where its audio "
   f"was cut from the song (grows every clip)")
ok(all(abs(drift_live[i]) <= abs(drift_live[i + 1]) + 1e-9 for i in range(N - 1)),
   "the error is CUMULATIVE, not a constant offset -- each clip is worse")

# With the rule applied (clip fills its slot exactly), drift is gone.
fixed_lengths = [CLIP] * N
drift_fixed = timeline_drift(planned, fixed_lengths, xfade_s=SONG_XFADE)
ok(max(abs(d) for d in drift_fixed) < 1.0 / SING_FPS,
   f"headroom-then-trim: worst drift {max(abs(d) for d in drift_fixed) * 1000:.1f}ms, "
   f"under one frame ({1000 / SING_FPS:.1f}ms)")

print("\n-- D: exact integer slots (never trust -t to fix a SHORT clip) --")
ok(exact_slot_frames(9.833333) == 236,
   f"9.8333s at 24fps is exactly {exact_slot_frames(9.833333)} frames")
ok(exact_slot_frames(210.0) == 5040, "the full 210s song is a 5040-frame grid")
lengths = [exact_slot_frames(d) / SING_FPS for d in (6.0, 6.0, 6.0)]
starts = assembled_start_times(lengths, xfade_s=XFADE_S)
ok(abs(starts[1] - (6.0 - XFADE_S)) < 1e-9,
   "clip 2 starts one fade earlier than its nominal position, as the concat does")

print("\n-- E: frames_for_slot always leaves enough to trim down to the slot --")
for slot in [4.0, 6.0, 7.0, 9.833, 9.9]:
    f = frames_for_slot(slot)
    survives = f / SING_FPS - TRIM_OVERHEAD_S
    ok(survives >= slot - 1e-6,
       f"slot {slot}s: render {f}f -> {survives:.3f}s survives the trim (>= slot, "
       f"so the fit is always a cut, never a frozen clone-pad)")

print("\n" + (f"ALL {PASS} PASS" if FAIL == 0 else f"{FAIL} FAIL / {PASS + FAIL} total"))
sys.exit(0 if FAIL == 0 else 1)
