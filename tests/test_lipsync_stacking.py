"""Native + MuseTalk stacking reversal (2026-08-05). NO GPU, NO renders, NO
MuseTalk venv needed.

Andrew reversed his 2026-06-19 "Native + MuseTalk both" instruction today.
Evidence (see ROLLBACK_MAP_2026-08-05.md finding d and this file's own
checks): the native-only chain.py render he approved this morning, the 23:27
crash inside the MuseTalk post-pass, and c134c63's finding that MuseTalk is
the wrong sync engine for creature faces regardless of stacking.

Two things are tested:
  1. `_should_run_musetalk_postpass` (import-light): the actual decision
     function, exercised directly with real booleans -- no source-text
     matching involved, this is real behavior.
  2. Source-text wiring checks (routes.py's default + comment, pipeline.py's
     call site) -- proving the decision function is actually reachable and
     wired in the right place, which an import-light test of the function in
     isolation cannot prove by itself.

RUN IT WITH:  python tests/test_lipsync_stacking.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from features.song_video.pipeline import _should_run_musetalk_postpass  # noqa: E402

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


print("\n-- A: _should_run_musetalk_postpass -- the real decision function, all 4 combos --")
ok(_should_run_musetalk_postpass(auto_lipsync=True, native_conditioned=True) is False,
   "auto_lipsync=True + native ran -> SKIP (the exact case this fix exists for -- "
   "an explicit auto_lipsync=true must NOT force the stack)")
ok(_should_run_musetalk_postpass(auto_lipsync=True, native_conditioned=False) is True,
   "auto_lipsync=True + no native conditioning -> RUN (MuseTalk stays reachable "
   "for jobs that never ran native conditioning at all)")
ok(_should_run_musetalk_postpass(auto_lipsync=False, native_conditioned=True) is False,
   "auto_lipsync=False + native ran -> SKIP (nobody asked for MuseTalk)")
ok(_should_run_musetalk_postpass(auto_lipsync=False, native_conditioned=False) is False,
   "auto_lipsync=False + no native conditioning -> SKIP (nobody asked for MuseTalk)")

print("\n-- B: return type discipline (a truthy/falsy leak would silently change "
     "'and not _stopped()' downstream) --")
ok(_should_run_musetalk_postpass(True, True) is False, "returns real bool False, not None/0")
ok(_should_run_musetalk_postpass(True, False) is True, "returns real bool True, not a truthy non-bool")

print("\n-- C: routes.py -- the /generate route's auto_lipsync default and its "
     "rewritten comment (source text) --")
_routes_path = os.path.join(os.path.dirname(__file__), "..", "features",
                            "song_video", "routes.py")
rsrc = open(_routes_path, encoding="utf-8").read()

ok(rsrc.count('"auto_lipsync":') == 2,
   "sanity: exactly two auto_lipsync call sites in routes.py (/generate and "
   "/batch/start) -- if this count ever changes the window-search below needs "
   "re-anchoring")

ok("REVERSED 2026-08-05" in rsrc,
   "a dated reversal marker exists in routes.py")
_rev_idx = rsrc.find("REVERSED 2026-08-05")
_rev_window = rsrc[_rev_idx:_rev_idx + 1600]

ok('"auto_lipsync":    bool(body.get("auto_lipsync", False))' in _rev_window,
   "the /generate route's auto_lipsync now defaults to False")
ok("23:27" in _rev_window, "the comment cites the 23:27 crash (inside this exact post-pass)")
ok("c134c63" in _rev_window, "the comment cites c134c63's creature-face finding")
ok("THIS MORNING" in _rev_window,
   "the comment cites the native-only render Andrew approved this morning")
ok("_should_run_musetalk_postpass" in _rev_window,
   "the comment points at the function that actually enforces the ruling")

# HISTORY MUST NOT BE DELETED -- the task's own hard requirement.
ok("Native + MuseTalk both" in rsrc,
   "Andrew's original 2026-06-19 instruction is still quoted verbatim, not deleted")
ok("abb7583" in rsrc, "the commit that introduced the original ON-by-default is still named")
ok("2026-06-19" in rsrc, "the original instruction's date is still on record")

print("\n-- D: KNOWN GAP, recorded not silently accepted -- /batch/start's own "
     "auto_lipsync default is untouched by this fix (task named routes.py:~300 "
     "only, i.e. the /generate route) --")
_batch_marker = '"lip_sync":       bool(body.get("lip_sync", False)),'
ok(_batch_marker in rsrc, "the /batch/start settings block still has its own lip_sync line (anchor)")
_batch_idx = rsrc.find(_batch_marker)
_batch_window = rsrc[_batch_idx:_batch_idx + 700]
ok('bool(body.get("auto_lipsync", False))' in _batch_window,
   "/batch/start's auto_lipsync now ALSO defaults False -- the builder flagged "
   "it as an out-of-scope inconsistency and the reviewer closed it same day "
   "(Andrew's reversal is a product ruling, not a per-route one)")

print("\n-- E: pipeline.py -- the post-pass call site actually gates through "
     "_should_run_musetalk_postpass, before lipsync_video ever runs (source text) --")
_pipeline_path = os.path.join(os.path.dirname(__file__), "..", "features",
                              "song_video", "pipeline.py")
psrc = open(_pipeline_path, encoding="utf-8").read()

ok(psrc.count("def _should_run_musetalk_postpass") == 1,
   "_should_run_musetalk_postpass is defined exactly once")

_parts = psrc.split("Lip sync post-pass (MuseTalk)", 1)
ok(len(_parts) == 2, "the MuseTalk post-pass section still exists in pipeline.py")
if len(_parts) == 2:
    _tail = _parts[1]
    _before_call = _tail.split("lipsync_video(", 1)[0]
    ok("_native_conditioned = bool(_lip_sync) and bool(_guide_audio)" in _before_call,
       "native-conditioned is computed from lip_sync AND the guide, exactly as "
       "the task specified ('lip_sync true and a guide was used')")
    ok('_should_run_musetalk_postpass(bool(settings.get("auto_lipsync", False)),'
       in _before_call,
       "the gate is called with the job's real auto_lipsync flag, before "
       "lipsync_video is ever invoked")
    ok("_native_conditioned)" in _before_call,
       "the gate is called with the computed native_conditioned flag")

print("\n" + (f"ALL {PASS} PASS" if FAIL == 0 else f"{FAIL} FAIL / {PASS + FAIL} total"))
sys.exit(0 if FAIL == 0 else 1)
