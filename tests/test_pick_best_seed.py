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

print("\n-- D: attempts are cleaned up, winner survives --")
leftovers = [f for f in os.listdir(tmp) if "_s" in f and f.endswith(".mp4")]
ok(len(leftovers) <= 3,
   f"non-winning attempt files are not accumulating without bound ({len(leftovers)})")

shutil.rmtree(tmp, ignore_errors=True)
print("\n" + (f"ALL {PASS} PASS" if FAIL == 0 else f"{FAIL} FAIL / {PASS + FAIL} total"))
sys.exit(0 if FAIL == 0 else 1)
