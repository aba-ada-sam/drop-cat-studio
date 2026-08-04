"""Calibration tests for features/song_video/artifact_screens.py.

NO GPU, NO WORKER, NO RENDERS. Every assertion runs against mp4s that already
exist on this box and were labelled BY EYE on 2026-08-04. That is the point:
the ledger rule is "any new metric/gate gets a ledger entry stating what
known-good and known-bad references it was validated against BEFORE first use.
No validation, no gating." This file is that validation, kept executable so a
future edit to the detectors has to keep reproducing the numbers a human
actually agreed with.

RUN IT WITH:  python tests/test_artifact_screens.py

Fixtures are session artifacts, not repo assets -- if they are gone, the file
SKIPS loudly rather than passing vacuously. A calibration test that silently
passes when its references are missing is worse than no test.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from features.song_video.artifact_screens import (  # noqa: E402
    red_series, ribbon_series, ribbon_verdict, summarize, pair_index_to_time,
)

SCRATCH = (r"C:\Users\andre\AppData\Local\Temp\claude\C--Users-andre"
           r"\b0293762-ce80-418c-84f3-73221223aaf5\scratchpad")
REVIEW = r"C:\Users\andre\Desktop\DCS_Review"
FIXED4 = os.path.join(REVIEW, "dcmvs_adam_friends_alien_FIXED4.mp4")

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


def need(path):
    if not os.path.isfile(path):
        print(f"SKIP: calibration reference missing: {path}")
        print("      (session artifact, not a repo asset -- nothing measured)")
        sys.exit(0)
    return path


need(FIXED4)

print("\n-- A: RIBBON reproduces its eye-calibrated band (FIXED4, both ends) --")
# Ledger: infested p95 1.07 vs clean p95 0.24, measured at full res on FIXED4.
bad = summarize(ribbon_series(FIXED4, 5.5, 1.5))     # Andrew's ribbons, eye-verified
good = summarize(ribbon_series(FIXED4, 20.0, 1.5))   # pod window 02, clean
ok(1.0 <= bad["p95"] <= 1.15,
   f"known-bad p95 {bad['p95']:.3f} reproduces the recorded 1.07")
ok(0.18 <= good["p95"] <= 0.30,
   f"clean-candidate p95 {good['p95']:.3f} reproduces the recorded 0.24")
ok(ribbon_verdict(bad["p95"], bad["max"]) == "infested",
   "the known-bad window is called INFESTED")
ok(ribbon_verdict(good["p95"], good["max"]) == "clean",
   "the clean window is called CLEAN")
ok(bad["p95"] > good["p95"] * 3,
   f"separation is wide, not marginal ({bad['p95']:.3f} vs {good['p95']:.3f})")

print("\n-- B: RED separates on p95 but OVERLAPS on max -- why it is a screen --")
red_bad = summarize(red_series(FIXED4, 155.04, 9.84))          # eye-verified strands
f031 = need(os.path.join(SCRATCH, "final_03_1.mp4"))
f070 = need(os.path.join(SCRATCH, "final_07_0.mp4"))
red_g1 = summarize(red_series(f031))
red_g2 = summarize(red_series(f070))   # the documented false-positive take
ok(0.13 <= red_bad["p95"] <= 0.19,
   f"known-bad red p95 {red_bad['p95']:.3f} reproduces the recorded 0.157")
ok(red_g1["p95"] < 0.10,
   f"clean take final_03_1 red p95 {red_g1['p95']:.3f} sits in the recorded 0.04-0.07 band")
# THE LOAD-BEARING ASSERTION. If this ever starts failing because the max
# ordering flipped, that is NOT permission to promote red to a gate -- it means
# the fixture set changed and the false-positive case needs re-establishing.
ok(red_g2["max"] >= red_bad["max"] * 0.95,
   f"eye-CLEAN final_07_0 red max {red_g2['max']:.3f} rivals/exceeds the "
   f"known-BAD {red_bad['max']:.3f} -- the documented false positive, reproduced")

print("\n-- C: the screen refuses to render a verdict for red --")
from features.song_video.artifact_screens import screen_window  # noqa: E402
sc = screen_window(f031)
ok(sc["red_verdict"] is None,
   "red_verdict is None by construction -- red ranks frames, it never rules")
ok(sc["ribbon_verdict"] in ("clean", "eye-check", "infested"),
   f"ribbon still yields its validated verdict ({sc['ribbon_verdict']})")
ok(len(sc["worst_red_pairs"]) > 0 and len(sc["worst_ribbon_pairs"]) > 0,
   "both screens surface worst frames for the eye regardless of verdict")

print("\n-- D: empty/undecodable input FAILS a gate, never passes it --")
empty = summarize(ribbon_series(os.path.join(SCRATCH, "does_not_exist.mp4")))
ok(empty["p95"] == 999.0 and ribbon_verdict(empty["p95"], empty["max"]) == "infested",
   "an undecodable clip scores 999 and is called infested, not clean")

print("\n-- E: pair index -> timestamp lands on the CHANGED frame --")
# ribbon_series index i is the pair (i, i+1); the artifact is on i+1. Dumping
# frame i instead shows the frame BEFORE the artifact and reads as clean.
ok(abs(pair_index_to_time(0, 10.0, 24.0) - (10.0 + 1 / 24.0)) < 1e-9,
   "pair 0 at window start 10.0s maps to 10.0 + 1/24s, not 10.0s")
ok(abs(pair_index_to_time(23, 0.0, 24.0) - 1.0) < 1e-9,
   "pair 23 maps to exactly 1.0s at 24fps")

print("\n" + (f"ALL {PASS} PASS" if FAIL == 0 else f"{FAIL} FAIL / {PASS + FAIL} total"))
sys.exit(0 if FAIL == 0 else 1)
