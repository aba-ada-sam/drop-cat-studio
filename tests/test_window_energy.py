"""Energy-vs-label window gate, tested against the REAL stem from the real bug.

NO GPU, NO RENDERS. Uses the isolated vocal stem of the song whose window map
was wrong (stem_vocals_hp.wav) plus generated audio for the edge cases.

The headline test is C: the four windows Studio hand-labelled "interlude" and
built as unconditioned filler are re-measured here, and this gate has to catch
them. If that test ever goes green-by-accident (e.g. someone loosens the floor),
79 seconds of undriven mouth becomes shippable again.

RUN IT WITH:  python tests/test_window_energy.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from features.song_video.window_energy import (  # noqa: E402
    VOICED_FLOOR_DBFS, WindowLabelDisagreement, check_plan, check_window,
    voiced_fraction, window_mean_dbfs,
)

SCRATCH = (r"C:\Users\andre\AppData\Local\Temp\claude\C--Users-andre"
           r"\b0293762-ce80-418c-84f3-73221223aaf5\scratchpad")
STEM = os.path.join(SCRATCH, "stem_vocals_hp.wav")

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


if not shutil.which("ffmpeg"):
    print("SKIP: ffmpeg not on PATH. Nothing measured.")
    sys.exit(0)

tmp = tempfile.mkdtemp(prefix="winenergy_")
loud = os.path.join(tmp, "loud.wav")
silent = os.path.join(tmp, "silent.wav")
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=44100",
                "-t", "6", "-c:a", "pcm_s16le", loud], capture_output=True, timeout=60)
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-t", "6", "-c:a", "pcm_s16le", silent], capture_output=True, timeout=60)

print("\n-- A: the measurement itself --")
ok(window_mean_dbfs(loud, 0.0, 5.0) > VOICED_FLOOR_DBFS, "audible audio reads above the floor")
lvl_sil = window_mean_dbfs(silent, 0.0, 5.0)
ok(lvl_sil is None or lvl_sil < VOICED_FLOOR_DBFS, "digital silence reads below the floor")
ok(window_mean_dbfs(os.path.join(tmp, "nope.wav"), 0.0, 5.0) is None,
   "unmeasurable input returns None, never a number that could read as silent")

print("\n-- B: unmeasurable audio fails TOWARD conditioning --")
r = check_window(os.path.join(tmp, "nope.wav"), 0.0, 5.0)
ok(r["must_condition"] is True and r["disagreement"],
   "a window we cannot measure is conditioned anyway, loudly -- never assumed silent")

print("\n-- C: THE REAL BUG. Studio's 'interlude' windows, re-measured --")
if not os.path.isfile(STEM):
    print(f"  SKIP: stem missing ({STEM}) -- cannot re-measure the real windows")
else:
    # The grid Studio built as unconditioned filler (24fps frame ranges -> seconds).
    INTERLUDES = {"02": (409 / 24, 882 / 24), "04": (1355 / 24, 1828 / 24),
                  "06": (2301 / 24, 2774 / 24), "08": (3247 / 24, 3721 / 24)}
    caught = 0
    for name, (t0, t1) in INTERLUDES.items():
        res = check_window(STEM, t0, t1, labelled_sung=False)
        flagged = res["must_condition"] and res["disagreement"] is not None
        caught += 1 if flagged else 0
        print(f"       window {name}: {res['mean_dbfs']:.1f} dBFS -> "
              f"{'FLAGGED' if flagged else 'passed as instrumental'}")
    ok(caught == len(INTERLUDES),
       f"all {len(INTERLUDES)} mislabelled 'interlude' windows are caught "
       f"({caught}/{len(INTERLUDES)}) -- the 79-second undriven-mouth bug, gated")

    # NEGATIVE CONTROLS, ASSERTED not printed. Red team, 2026-08-04: group C
    # alone is trivially satisfiable -- a gate hardwired to "always condition"
    # passes it. Without a real-song negative, any floor in a ~60 dB range
    # passes the suite. These pin the other end against REAL audio.
    outro = check_window(STEM, 197.0, 209.0, labelled_sung=False)
    ok(outro["must_condition"] is False and outro["disagreement"] is None,
       f"the real outro (vocals end ~192s) rests: energy in "
       f"{outro['energy_voiced_frac'] * 100:.0f}% of it, no false alarm")
    intro = check_window(STEM, 0.0, 4.0, labelled_sung=False)
    ok(intro["must_condition"] is False,
       f"the real song intro rests too ({intro['energy_voiced_frac'] * 100:.0f}% "
       f"energy) -- the gate discriminates on real audio, not just on silence")

    # THE DILUTION REGRESSION (red team H1). At the real 19.7s production window
    # size, a mean-based check reads -42.6 dBFS on this window and calls it
    # instrumental -- rebuilding the exact 79-second undriven-mouth bug this
    # module exists to prevent. The fraction-based check must catch it.
    w10 = check_window(STEM, 174.71, 194.42, labelled_sung=False)
    ok(w10["must_condition"] is True,
       f"w10 (174.7-194.4s) is CONDITIONED: {w10['energy_voiced_frac'] * 100:.0f}% "
       f"of it carries energy, peak {w10['peak_dbfs']:.1f} dBFS -- a window mean "
       f"({w10['mean_dbfs']:.1f}) would have called this instrumental")
    sub = check_window(STEM, 184.0, 185.0)
    ok(sub["must_condition"] == w10["must_condition"],
       "a strict SUBSET of that window agrees with it -- no self-contradiction")

print("\n-- C2: energy may ADD conditioning but must never REMOVE it --")
if os.path.isfile(STEM):
    # Ledger rule is one-directional: "any window ABOVE the floor MUST be
    # conditioned no matter what any map says." The reverse was never
    # authorized. An earlier draft enforced it and would override a CORRECT
    # human label, telling the operator the map was stale when it was right.
    r = check_window(STEM, 197.0, 209.0, labelled_sung=True)
    ok(r["must_condition"] is True,
       "a quiet window LABELLED sung is still conditioned -- the label wins")
    ok(r["disagreement"] is not None,
       "...and the map/song conflict is still reported rather than swallowed")
    r2 = check_window(STEM, 197.0, 209.0, intervals=[(197.0, 209.0)])
    ok(r2["must_condition"] is True,
       "a quiet window the VAD calls fully voiced is still conditioned")

print("\n-- D: a genuinely instrumental window still conditions on silence --")
r = check_window(silent, 0.0, 5.0, labelled_sung=False)
ok(r["must_condition"] is False and r["disagreement"] is None,
   "silence + instrumental label agree -> resting mouth, no false alarm")

print("\n-- E: VAD under-detection is caught even with no label at all --")
r = check_window(loud, 0.0, 5.0, intervals=[], labelled_sung=None)
ok(r["must_condition"] and r["disagreement"] and "VAD" in r["disagreement"],
   "audible stem + 0% VAD-voiced is flagged as the detector under-reading")

print("\n-- F: voiced_fraction math --")
ok(abs(voiced_fraction([(0.0, 5.0)], 0.0, 10.0) - 0.5) < 1e-9, "half-covered window = 0.5")
ok(voiced_fraction([], 0.0, 10.0) == 0.0, "no intervals = 0.0")
ok(abs(voiced_fraction([(-5.0, 15.0)], 0.0, 10.0) - 1.0) < 1e-9,
   "an interval overhanging both ends clamps to 1.0, never above")
# The clamp only actually MATTERS for overlapping intervals -- per-interval
# clipping already handles overhang, so the assertion above passes even with
# the clamp deleted (red team L5). This is the case that needs it.
ok(abs(voiced_fraction([(0.0, 10.0), (0.0, 10.0)], 0.0, 10.0) - 1.0) < 1e-9,
   "two OVERLAPPING full-coverage intervals still give 1.0, not 2.0")

print("\n-- G: strict mode raises instead of logging (unattended paths) --")
try:
    check_plan(loud, [{"t0": 0.0, "t1": 5.0, "labelled_sung": False}],
               strict=True, log=lambda m: None)
    ok(False, "strict=True should have raised on a disagreement")
except WindowLabelDisagreement:
    ok(True, "strict=True raises WindowLabelDisagreement instead of logging and moving on")

msgs = []
check_plan(loud, [{"t0": 0.0, "t1": 5.0, "labelled_sung": False}], log=msgs.append)
ok(any("DISAGREEMENT" in m for m in msgs), "non-strict mode still logs loudly")

shutil.rmtree(tmp, ignore_errors=True)
print("\n" + (f"ALL {PASS} PASS" if FAIL == 0 else f"{FAIL} FAIL / {PASS + FAIL} total"))
sys.exit(0 if FAIL == 0 else 1)
