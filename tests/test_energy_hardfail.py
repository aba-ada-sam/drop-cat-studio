"""Energy-AT-CREATION hard-fail: assert_stem_energy / assert_slice_energy.

NO GPU, NO renders, NO worker. Companion to test_window_energy.py (which
tests the PER-WINDOW "is this window sung" gate) and test_guide_hardfail.py
(which tests that a FAILED isolation raises instead of returning the raw
mix). This file tests the third leg: isolation can SUCCEED and still hand
back audio with no signal in it -- a near-empty Demucs stem, a muted source,
a bad ffmpeg cut -- and that is invisible to every downstream metric, because
a still mouth conditioned on silence is judged correct against the silence it
was given. See GuideSilentError's docstring in window_energy.py for the three
2026-08-04 incidents that share this shape.

All fixtures are REAL wavs generated with ffmpeg into a tempdir -- no mocked
audio, no canned measurement dicts. Group B in particular computes its
expected voiced_frac from the actual construction parameters (tone seconds /
total seconds) rather than asserting a hardcoded number, so the test proves
the fraction math, not just "it didn't raise".

RUN IT WITH:  python tests/test_energy_hardfail.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from features.song_video.window_energy import (  # noqa: E402
    GUIDE_MIN_PEAK_DBFS, GuideSilentError, assert_slice_energy, assert_stem_energy,
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


if not shutil.which("ffmpeg"):
    print("SKIP: ffmpeg not on PATH. Nothing measured.")
    sys.exit(0)

tmp = tempfile.mkdtemp(prefix="energyhardfail_")


def _ffmpeg(args, timeout=60):
    return subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args,
                          capture_output=True, timeout=timeout)


# -- Fixtures, all built with real ffmpeg -------------------------------------

# (a) Pure digital silence, 10s, stereo (same recipe as test_window_energy.py's
# "silent.wav" so the two suites agree on what "digitally silent" means).
silence_wav = os.path.join(tmp, "silence10.wav")
_ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "10",
        "-c:a", "pcm_s16le", silence_wav])

# (b) 8s of an audible tone followed by 2s of true digital silence, 10s total,
# both segments mono so the concat filter's channel layouts match. The tone
# is attenuated (-20dB off the sine source's own level) rather than left at
# full scale -- comfortably above the floor without being suspiciously loud.
# Boundaries land exactly on 0.5s probe-grid multiples (8.0s / 2.0s at
# 44100Hz -- both a whole number of samples), so no probe straddles the cut.
TONE_S, TOTAL_S = 8.0, 10.0
tone_then_silence_wav = os.path.join(tmp, "tone_then_silence.wav")
_ffmpeg(["-f", "lavfi", "-t", f"{TONE_S:.0f}", "-i", "sine=frequency=220:sample_rate=44100",
        "-f", "lavfi", "-t", f"{TOTAL_S - TONE_S:.0f}", "-i", "anullsrc=r=44100:cl=mono",
        "-filter_complex", "[0:a]volume=-20dB[a0];[a0][1:a]concat=n=2:v=0:a=1[out]",
        "-map", "[out]", "-c:a", "pcm_s16le", tone_then_silence_wav])

# (c) Full-file low-level noise, 10s -- broadband (not a quiet tone) so very
# low amplitude doesn't collapse into a handful of repeating quantization
# steps. Attenuated hard enough that it must land under the -55 dBFS peak
# floor regardless of the noise source's own reference level.
noise_wav = os.path.join(tmp, "noise70.wav")
_r = _ffmpeg(["-f", "lavfi", "-i", "anoisesrc=color=white:amplitude=1:sample_rate=44100",
             "-t", "10", "-af", "volume=-70dB", "-c:a", "pcm_s16le", noise_wav])
if _r.returncode != 0 or not os.path.isfile(noise_wav):
    # Fallback for an ffmpeg build without anoisesrc -- a heavily attenuated
    # tone still lands well under the floor, just with less realistic
    # spectral shape. Either way the fixture must land under GUIDE_MIN_PEAK_DBFS.
    _ffmpeg(["-f", "lavfi", "-i", "sine=frequency=333:sample_rate=44100",
            "-t", "10", "-af", "volume=-70dB", "-c:a", "pcm_s16le", noise_wav])

missing_wav = os.path.join(tmp, "does_not_exist.wav")  # never created, on purpose

print("\n-- A: pure digital silence -> assert_stem_energy raises --")
try:
    assert_stem_energy(silence_wav, log=lambda m: None)
    ok(False, "10s of digital silence should have raised GuideSilentError")
except GuideSilentError as e:
    ok(True, "digital silence raises GuideSilentError")
    ok(str(e), "the exception carries a message (not raised bare)")

print("\n-- B: audible tone + silence -> passes, voiced_frac matches the "
     "construction honestly --")
expected_frac = TONE_S / TOTAL_S
try:
    prof = assert_stem_energy(tone_then_silence_wav, log=lambda m: None)
    ok(True, f"{TONE_S:.0f}s tone + {TOTAL_S - TONE_S:.0f}s silence does NOT raise")
    ok(prof is not None and prof.get("voiced_frac") is not None,
       "returns a real measurement dict with a numeric voiced_frac, not None")
    if prof is not None and prof.get("voiced_frac") is not None:
        # Computed from the fixture's own construction (tone_s / total_s), not
        # a hardcoded "0.8" -- this is what "honestly" means here: if the
        # fixture parameters above ever change, this expectation moves with
        # them instead of silently drifting out of sync.
        ok(abs(prof["voiced_frac"] - expected_frac) <= 0.1,
           f"voiced_frac {prof['voiced_frac']:.3f} ~= {TONE_S:.0f}/{TOTAL_S:.0f} "
           f"= {expected_frac:.3f} (tone-seconds / total-seconds), within tolerance")
        ok(prof["peak_dbfs"] is not None and prof["peak_dbfs"] > GUIDE_MIN_PEAK_DBFS,
           f"peak {prof['peak_dbfs']} dBFS clears the {GUIDE_MIN_PEAK_DBFS} floor "
           f"with real margin")
except GuideSilentError as e:
    ok(False, f"should NOT have raised on an audible tone: {e}")

print("\n-- C: full-file low-level noise (below the peak floor) -> raises --")
try:
    assert_stem_energy(noise_wav, log=lambda m: None)
    ok(False, f"noise attenuated below {GUIDE_MIN_PEAK_DBFS} dBFS should have raised")
except GuideSilentError as e:
    ok(True, "sub-floor noise raises GuideSilentError")
    ok("SILENT" in str(e), "the message says SILENT, not some generic error")

print("\n-- D: unmeasurable / missing file -> raises, never passes quietly --")
try:
    assert_stem_energy(missing_wav, log=lambda m: None)
    ok(False, "a missing file should raise -- no information is not evidence of energy")
except GuideSilentError as e:
    ok(True, "a missing/unmeasurable file raises GuideSilentError")
    ok("unmeasurable" in str(e).lower(),
       "the message says it could not be measured, not that it was silent "
       "(the two are different failure classes and the message should say which)")

print("\n-- E: assert_slice_energy(voiced=False) on silence -> returns None, "
     "no raise --")
r = assert_slice_energy(silence_wav, 3, voiced=False, log=lambda m: None)
ok(r is None, "an UNVOICED window's slice is never asserted -- silence is its "
              "correct content, not a bug")

print("\n-- F: assert_slice_energy(voiced=True) on silence -> raises, naming "
     "the clip index --")
try:
    assert_slice_energy(silence_wav, 7, voiced=True, log=lambda m: None)
    ok(False, "a VOICED window's silent slice should raise")
except GuideSilentError as e:
    ok(True, "a voiced window's silent slice raises GuideSilentError")
    ok("clip 7" in str(e), f"the message names the clip index (clip 7): {e}")

print("\n-- G: static wiring check -- both call sites exist in pipeline.py "
     "(source text only, pipeline.py is NOT imported here) --")
_pipeline_path = os.path.join(os.path.dirname(__file__), "..", "features",
                              "song_video", "pipeline.py")
psrc = open(_pipeline_path, encoding="utf-8").read()

ok("assert_stem_energy" in psrc, "pipeline.py imports/calls assert_stem_energy")
ok("assert_slice_energy" in psrc, "pipeline.py imports/calls assert_slice_energy")
ok("GuideSilentError" in psrc, "pipeline.py references GuideSilentError")

# Whole-stem call site: must run right after _isolate_guide_vocals's try/except
# settles (its OWN try/except, deliberately -- mirroring the explicit-vs-
# implicit gate rather than sharing the isolation except block; see the block
# comment in pipeline.py for why), guarded so it is skipped when isolation
# already degraded to _guide_audio=None. Anchored on the full ASSIGNMENT
# line, not just the bare call text -- the function's own
# `def _isolate_guide_vocals(audio_wav, job_dir):` contains the same call
# substring and sits earlier in the file, so splitting on the bare call would
# silently anchor on the def instead of the call site.
_call_line = "_guide_audio, _guide_intervals = _isolate_guide_vocals(audio_wav, job_dir)"
_after_isolate = psrc.split(_call_line, 1)
ok(len(_after_isolate) == 2, "the _isolate_guide_vocals call site still exists in pipeline.py")
if len(_after_isolate) == 2:
    _tail = _after_isolate[1]
    # Bounded by the NEXT major section marker (the window-rule comment) so
    # this window covers both the isolation except AND the new energy check
    # that follows it, however long either grows.
    _window_marker = "The window rule, mechanised"
    _between = _tail.split(_window_marker, 1)[0] if _window_marker in _tail else _tail[:2500]
    ok("assert_stem_energy(" in _between,
       "assert_stem_energy is called shortly after _isolate_guide_vocals, "
       "before the per-window energy check")
    ok("if _guide_audio is not None:" in _between,
       "the whole-stem check is skipped when isolation already degraded to "
       "no guide -- it must not re-check (or crash on) an already-None guide")
    ok("except GuideSilentError as e:" in _between,
       "the whole-stem call site catches GuideSilentError with its own "
       "explicit-vs-implicit gate (not sharing the isolation except block, "
       "which test_guide_hardfail.py pins to a fixed-offset text window that "
       "adding code inside it would silently break)")

# Per-slice call site: inside the pre-extraction loop, gated on the per-window
# voiced verdict, with its own explicit-vs-implicit degrade (only THIS clip's
# slice, never the raw mix). Bounded by the loop's own start/end log lines
# (not a fixed character count, which is brittle against comment edits).
_pre = psrc.split('Lip sync ON -- pre-extracting', 1)
ok(len(_pre) == 2, "the audio-slice pre-extraction loop still exists in pipeline.py")
if len(_pre) == 2:
    _tail2 = _pre[1].split("Audio slices ready", 1)[0]
    ok("assert_slice_energy(" in _tail2,
       "assert_slice_energy is called inside the pre-extraction loop")
    ok("_voiced_window[" in _tail2,
       "the per-slice call site reads the per-window voiced verdict (_voiced_window)")
    ok("except GuideSilentError as e:" in _tail2,
       "the per-slice call site catches GuideSilentError explicitly (loop-scoped, "
       "cannot rely on the outer try/except above it)")

# The propagation rule itself, checked structurally rather than trusting the
# prose above: BOTH hard-fail sites must gate on the explicit/implicit flag,
# not just the first one that existed before this change.
ok(psrc.count("if _lip_sync_explicit:") >= 2,
   "both the whole-stem and per-slice silent-guide paths gate on "
   "_lip_sync_explicit (explicit request hard-fails, implicit default degrades)")
# Each "if _lip_sync_explicit:" must be followed by a bare `raise` on the very
# next line -- not just present somewhere in the file (which "raise" appearing
# dozens of times elsewhere for unrelated errors would satisfy trivially).
_explicit_sites = [i for i in range(len(psrc)) if psrc.startswith("if _lip_sync_explicit:", i)]
_reraise_count = sum(1 for i in _explicit_sites
                     if psrc[i:i + 200].split("\n", 2)[1].strip() == "raise")
ok(_reraise_count >= 2,
   f"{_reraise_count} of {len(_explicit_sites)} 'if _lip_sync_explicit:' site(s) "
   f"re-raise on the immediate next line -- an explicit request must hard-fail, "
   f"not degrade")

print("\n" + (f"ALL {PASS} PASS" if FAIL == 0 else f"{FAIL} FAIL / {PASS + FAIL} total"))
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if FAIL == 0 else 1)
