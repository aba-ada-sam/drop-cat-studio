"""The conditioning-audio hard-fail. NO GPU, NO renders, NO worker.

LIPSYNC_LEDGER design rule (2026-08-04): "the render path must not ACCEPT a
raw-mix wav -- prep produces a stem artifact, renderers take only that."

Before this, six separate paths silently degraded a lip-sync job to
unconditioned or full-mix audio and still reported success. Every one produced
a FINISHED VIDEO whose only symptom was that nobody was singing -- invisible to
every metric, discoverable only by a human watching the whole song. These tests
pin the refusals so none of them can quietly come back.

RUN IT WITH:  python tests/test_guide_hardfail.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


from features.song_video import pipeline  # noqa: E402
from features.song_video.pipeline import GuideIsolationError  # noqa: E402

print("\n-- A: isolation failures RAISE; they never return the raw mix --")
ok(issubclass(GuideIsolationError, Exception),
   "GuideIsolationError exists and is raisable")

tmp = tempfile.mkdtemp(prefix="guardfail_")
from pathlib import Path  # noqa: E402


class _FakePath:
    """Stands in for the demucs-capable venv python that _paths() returns."""
    def __init__(self, exists):
        self._exists = exists

    def is_file(self):
        return self._exists


import features.song_video.vocal_isolation as _runner  # noqa: E402

_orig_paths = _runner._paths
_orig_sep = _runner._separate_vocals
try:
    # 1. Missing venv -- used to log "conditioning on full mix" and return None.
    _runner._paths = lambda: (Path(tmp), _FakePath(False))
    try:
        pipeline._isolate_guide_vocals("whatever.wav", Path(tmp))
        ok(False, "missing demucs-capable venv should raise, not fall back")
    except GuideIsolationError as e:
        ok("full mix" in str(e).lower() or "isolat" in str(e).lower(),
           "missing venv RAISES and explains why the full mix is not acceptable")

    # 2. Demucs produces nothing -- used to return None.
    _runner._paths = lambda: (Path(tmp), _FakePath(True))
    _runner._separate_vocals = lambda py, src, dst: False
    try:
        pipeline._isolate_guide_vocals("whatever.wav", Path(tmp))
        ok(False, "failed separation should raise, not fall back")
    except GuideIsolationError:
        ok(True, "Demucs failure RAISES rather than conditioning on the mix")

    # 3. Demucs raises -- used to be swallowed by a bare `except Exception`.
    def _boom(py, src, dst):
        raise RuntimeError("demucs exploded")
    _runner._separate_vocals = _boom
    try:
        pipeline._isolate_guide_vocals("whatever.wav", Path(tmp))
        ok(False, "an exception inside separation should surface, not fall back")
    except GuideIsolationError as e:
        ok("demucs exploded" in str(e),
           "an exception inside separation surfaces WITH its cause attached")
finally:
    _runner._paths = _orig_paths
    _runner._separate_vocals = _orig_sep

print("\n-- A2: EXPLICIT request hard-fails; the mere DEFAULT degrades --")
# lip_sync DEFAULTS TO TRUE in _do_song_gpu_phase, so an ordinary song-video job
# on a box with no demucs-capable venv reaches the isolation call without anyone
# having asked for lip sync. Hard-failing those would break working jobs -- a
# worse regression than the bug being fixed. Caught while wiring, before ship.
psrc = open(os.path.join(os.path.dirname(__file__), "..", "features", "song_video",
                         "pipeline.py"), encoding="utf-8").read()
ok('_lip_sync_explicit = "lip_sync" in settings' in psrc,
   "the code distinguishes an EXPLICIT lip_sync request from the default")
ok("if _lip_sync_explicit:\n                raise" in psrc,
   "an EXPLICIT request re-raises -- the user asked for it and must not get a "
   "silent statue")
_deg = psrc.split("_lip_sync_explicit", 1)[1][:1200]
ok("_guide_audio = None" in _deg and "_lip_sync = False" in _deg,
   "the implicit default degrades to an UNCONDITIONED render (guide=None), "
   "not to the raw mix")
ok("_guide_audio = audio_wav" not in _deg,
   "the degrade path never reassigns the raw mix as the guide")

print("\n-- B: no silent-fallback language survives in the render path --")
src = open(os.path.join(os.path.dirname(__file__), "..", "features", "song_video",
                        "pipeline.py"), encoding="utf-8").read()
for phrase in ("conditioning on full mix", "-- conditioning on full mix"):
    ok(phrase not in src, f"pipeline.py no longer contains {phrase!r}")

wsrc = open(os.path.join(os.path.dirname(__file__), "..", "services",
                         "wangp_worker.py"), encoding="utf-8").read()
ok("retrying without audio" not in wsrc,
   "the worker no longer retries with audio conditioning stripped")
ok("generating without lip sync" not in wsrc,
   "...and no longer reports success for an unconditioned render")
ok("Refusing to" in wsrc,
   "the worker now REFUSES an audio-conditioned request it cannot queue, "
   "and says what to change")

print("\n-- C: the refusal explains the actual likely cause --")
ok("64-multiple" in wsrc or "64-multiples" in wsrc,
   "the worker's refusal names the resolution rounding that usually causes it, "
   "so the reader can act instead of just retrying")

print("\n" + (f"ALL {PASS} PASS" if FAIL == 0 else f"{FAIL} FAIL / {PASS + FAIL} total"))
sys.exit(0 if FAIL == 0 else 1)
