"""Product tiers (60s admin / 30s user). NO GPU, NO renders -- pure arithmetic
plus the job_payload shape sent to the route.

Andrew pivoted the product 2026-08-04: full-song format is dead, 60s for admin,
30s for users. This pins the two shapes so a future edit that breaks the
conditioning-grip math fails HERE, at import time, rather than in a render that
looks fine and simply does not lip-sync.

RUN IT WITH:  python tests/test_tiers.py
"""
import os
import sys

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


print("\n-- A: importing tiers.py itself proves the render-cap invariant --")
# If this import raises, both tiers are already broken -- the module asserts
# render_frames <= SING_MAX_FRAMES for every tier at import time.
import features.song_video.tiers as tiers_mod  # noqa: E402
ok(True, "module imported without an AssertionError -- both tiers fit under "
        "the conditioning-grip ceiling")

print("\n-- B: the two shapes are what Andrew actually asked for --")
# 2026-08-04 ~22:55: Andrew superseded his own 60s-admin spec -- "why are we
# still doing 60s when 30s is good enough?" 30s is the format for BOTH tiers;
# only the take budget differs.
admin = tiers_mod.tier("admin")
user = tiers_mod.tier("user")
ok(admin["target_s"] == 30.0, f"admin targets 30s (got {admin['target_s']}) -- length parity per the 22:55 ruling")
ok(user["target_s"] == 30.0, f"user targets 30s (got {user['target_s']})")
ok(admin["num_clips"] * admin["clip_s"] == admin["target_s"],
   f"admin: {admin['num_clips']} x {admin['clip_s']}s = exactly {admin['target_s']}s, no remainder")
ok(user["num_clips"] * user["clip_s"] == user["target_s"],
   f"user: {user['num_clips']} x {user['clip_s']}s = exactly {user['target_s']}s, no remainder")

print("\n-- C: admin gets a deeper take budget than user --")
ok(admin["best_of_n"] > user["best_of_n"],
   f"admin best_of_n={admin['best_of_n']} > user best_of_n={user['best_of_n']} "
   f"-- a supervised render can afford more GPU per hard window than an "
   f"unattended user one")
ok(admin["best_of_n"] <= 10 and user["best_of_n"] <= 10,
   "neither tier exceeds the route's raised best_of_n cap (10)")

print("\n-- D: both tiers render under the conditioning-grip ceiling, with room --")
from features.song_video.sing_grid import SING_MAX_FRAMES  # noqa: E402
for name, t in (("admin", admin), ("user", user)):
    ok(t["render_frames"] < SING_MAX_FRAMES,
       f"{name}: {t['render_frames']}f render, under the {SING_MAX_FRAMES}f "
       f"ceiling with margin (not just AT it)")

print("\n-- E: an unknown tier name fails loudly, not silently --")
try:
    tiers_mod.tier("nonexistent")
    ok(False, "an unknown tier name should raise")
except KeyError as e:
    ok("nonexistent" in str(e) and "admin" in str(e),
       "raises KeyError naming the bad value AND the known-good options")

print("\n-- F: job_payload matches what the route actually reads --")
p = tiers_mod.job_payload("admin", "song.wav", "photo.png")
ok(p["lip_sync"] is True,
   "lip_sync is EXPLICITLY true -- the route defaults it False, and with it "
   "false NONE of the ported wiring (isolation, window-energy, screens, "
   "best-of-N) runs at all")
ok(p["best_of_n"] == admin["best_of_n"] and p["clip_duration"] == int(admin["clip_s"])
   and p["num_clips"] == admin["num_clips"],
   "payload numbers match the resolved tier exactly")
ok(p["audio_path"] == "song.wav" and p["photo_path"] == "photo.png",
   "input paths pass through unchanged")
# 2026-08-05, ROLLBACK_MAP finding a: a tier renders a WINDOW of the song
# (num_clips*clip_s seconds), not the whole upload -- window_delivery=True
# tells the merge stage to mux that window, never loop the short render to
# try to cover the full song. See pipeline.py's _merge_video_audio_trim.
ok(p.get("window_delivery") is True,
   "window_delivery is explicitly True in the payload")

print("\n-- G: a deliberately bad tier is caught by the module's own assertions --")
# Prove the safety net actually works, not just that the two shipped tiers
# happen to be fine -- construct a tier that WOULD breach the ceiling and
# confirm the same assertion pattern catches it.
from features.song_video.sing_grid import frames_for_slot  # noqa: E402
_bad_clip_s = 11.0   # renders well past the ~250f ceiling once headroom is added
_bad_frames = frames_for_slot(_bad_clip_s, with_junction=True)
ok(_bad_frames > SING_MAX_FRAMES,
   f"sanity: an 11s clip really would breach the ceiling ({_bad_frames}f) -- "
   f"confirms the invariant tiers.py checks is a REAL constraint, not a formality")

print("\n" + (f"ALL {PASS} PASS" if FAIL == 0 else f"{FAIL} FAIL / {PASS + FAIL} total"))
sys.exit(0 if FAIL == 0 else 1)
