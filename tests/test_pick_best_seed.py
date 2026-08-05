"""_pick_best_seed driven for real, with a fake generator. NO GPU, NO worker.

The selection loop is where the artifact screen, the sync ranking and the
early-accept interact, and all three of the wired-path HIGH defects lived in
exactly that kind of seam rather than inside any module. So this drives the
REAL function with a gen_fn that copies known-verdict mp4s into place.

The load-bearing case is B: when every take screens as infested, the function
must still return a clip. Returning None makes the caller read the clip as a
DEAD RENDER -- it restarts the WanGP worker, waits up to 90s, and re-renders
once with NO screen at all -- so the screen would discard N real takes and then
ship an unscreened one. Measured infest rate on real takes is ~41%, which at
best_of_n=3 puts a 12-clip job at better-than-even odds of hitting it.

RUN IT WITH:  python tests/test_pick_best_seed.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCRATCH = (r"C:\Users\andre\AppData\Local\Temp\claude\C--Users-andre"
           r"\b0293762-ce80-418c-84f3-73221223aaf5\scratchpad")
CLEAN = os.path.join(SCRATCH, "final_03_1.mp4")          # eye-verified clean
DIRTY = os.path.join(SCRATCH, "quarantine", "final_01_1.mp4")  # quarantined

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


for f in (CLEAN, DIRTY):
    if not os.path.isfile(f):
        print(f"SKIP: fixture missing ({f}) -- nothing measured")
        sys.exit(0)

from features.song_video import pipeline  # noqa: E402
from features.song_video.artifact_screens import (  # noqa: E402
    ribbon_series, ribbon_verdict, summarize,
)

# Establish what the screen actually says about each fixture, so the test
# asserts against measured reality rather than an assumed label.
_c = summarize(ribbon_series(CLEAN))
_d = summarize(ribbon_series(DIRTY))
V_CLEAN = ribbon_verdict(_c["p95"], _c["max"])
V_DIRTY = ribbon_verdict(_d["p95"], _d["max"])
print(f"\nfixture verdicts: clean-take={V_CLEAN} (p95 {_c['p95']:.2f}) "
      f"dirty-take={V_DIRTY} (p95 {_d['p95']:.2f})")

tmp = tempfile.mkdtemp(prefix="pbs_")
logs: list[str] = []


def make_gen(src):
    calls = {"n": 0}

    def gen_fn(seed, attempt_path):
        calls["n"] += 1
        shutil.copy2(src, attempt_path)
        return attempt_path
    return gen_fn, calls


print("\n-- A: a clean take is selectable and the loop terminates --")
out = os.path.join(tmp, "clipA.mp4")
gen, calls = make_gen(CLEAN)
res = pipeline._pick_best_seed(0, out, gen, None, 1234, 3, log_fn=logs.append)
ok(res is not None and os.path.isfile(res),
   f"returned a usable clip after {calls['n']} take(s)")

print("\n-- B: ALL takes screened out -> still returns a clip, loudly flagged --")
if V_DIRTY != "infested":
    print(f"  SKIP: the dirty fixture screens as {V_DIRTY}, not infested -- "
          f"cannot exercise the all-rejected path with it")
else:
    logs.clear()
    out = os.path.join(tmp, "clipB.mp4")
    gen, calls = make_gen(DIRTY)
    res = pipeline._pick_best_seed(1, out, gen, None, 999, 3, log_fn=logs.append)
    ok(res is not None and os.path.isfile(res),
       "returns a clip instead of None -- no bogus 'dead render' -> worker "
       "restart -> UNSCREENED re-render")
    ok(any("LOOK AT THIS CLIP" in m for m in logs),
       "and says loudly that every take was rejected, so it reaches a human")
    ok(calls["n"] == 3, f"all 3 takes were generated and considered ({calls['n']})")

print("\n-- C: a decode failure is not reported as an artifact verdict --")
logs.clear()
out = os.path.join(tmp, "clipC.mp4")


def gen_broken(seed, attempt_path):
    open(attempt_path, "wb").close()   # zero-byte: undecodable
    return attempt_path


res = pipeline._pick_best_seed(2, out, gen_broken, None, 7, 2, log_fn=logs.append)
ok(any("could not decode" in m for m in logs),
   "an undecodable take is reported as a DECODE failure, not as "
   "'ribbon artifacts (p95 999.00)'")
ok(not any("ribbon artifacts" in m for m in logs),
   "...and is never given a confident artifact diagnosis it cannot support")

print("\n-- E: sync floor is ADVISORY by default (disarmed 2026-08-04 late) --")
# Ground truth inverted the gate on first human contact: Andrew's one approved
# take (static, rank 0.001) is refused by it, and the three takes that clear it
# he rejected. While the scorer is uncalibrated, require_sync=True must bank
# the best take anyway and log what enforcement WOULD have done -- evidence
# accumulates, nothing is refused, no raise.
from features.song_video.pipeline import (  # noqa: E402
    SYNC_ENFORCE, SYNC_RANK_FLOOR, SyncFloorNotMet,
)

ok(SYNC_ENFORCE is False,
   "SYNC_ENFORCE ships False -- re-arming requires a recalibrated scorer in the "
   "same commit, never a lone flag flip")
logs.clear()
out = os.path.join(tmp, "clipE.mp4")
gen, calls = make_gen(CLEAN)   # clean, but with audio_slice=None it cannot sync
res = pipeline._pick_best_seed(3, out, gen, None, 4242, 3,
                               log_fn=logs.append, require_sync=True)
ok(res is not None and os.path.isfile(res),
   "advisory mode BANKS the take on a voiced window instead of raising")
ok(any("WOULD-HAVE-REFUSED" in m for m in logs),
   "and logs what enforcement would have done, so the evidence keeps building")

print("\n-- E-armed: enforcement still works when explicitly re-armed --")
logs.clear()
out = os.path.join(tmp, "clipEarmed.mp4")
gen, calls = make_gen(CLEAN)
pipeline.SYNC_ENFORCE = True
try:
    pipeline._pick_best_seed(5, out, gen, None, 4242, 3,
                             log_fn=logs.append, require_sync=True)
    ok(False, "armed: a voiced window with no synced take must NOT bank a clip")
except SyncFloorNotMet as e:
    ok("VOICED" in str(e) and "still mouth" in str(e),
       "armed: raises SyncFloorNotMet, refusing to bank a still mouth over singing")
    ok(calls["n"] == 3, f"armed: it spent its whole take budget first ({calls['n']}/3)")
finally:
    pipeline.SYNC_ENFORCE = False
ok(any("below the sync floor" in m for m in logs),
   "armed: each rejected take says WHY (synced flag + rank vs floor)")

print("\n-- E2: the SAME takes are acceptable on an INSTRUMENTAL window --")
logs.clear()
out = os.path.join(tmp, "clipE2.mp4")
gen, calls = make_gen(CLEAN)
res = pipeline._pick_best_seed(4, out, gen, None, 4242, 3,
                              log_fn=logs.append, require_sync=False)
ok(res is not None and os.path.isfile(res),
   "an unvoiced window banks the clean static take -- a resting mouth through "
   "an instrumental bar is the CORRECT content, not a failure")

print("\n-- E3: SyncFloorNotMet is distinct from a dead renderer --")
ok(SyncFloorNotMet is not None and issubclass(SyncFloorNotMet, RuntimeError),
   "it is a real exception type, not a None return -- so the caller cannot "
   "mistake it for a dead render and answer with a worker restart plus an "
   "unscreened re-render")
ok(0.0 < SYNC_RANK_FLOOR < 1.0, f"the floor is a sane rank ({SYNC_RANK_FLOOR})")

print("\n-- D: attempts are cleaned up, winner survives --")
leftovers = [f for f in os.listdir(tmp) if "_s" in f and f.endswith(".mp4")]
ok(len(leftovers) <= 3,
   f"non-winning attempt files are not accumulating without bound ({len(leftovers)})")

shutil.rmtree(tmp, ignore_errors=True)
print("\n" + (f"ALL {PASS} PASS" if FAIL == 0 else f"{FAIL} FAIL / {PASS + FAIL} total"))
sys.exit(0 if FAIL == 0 else 1)
