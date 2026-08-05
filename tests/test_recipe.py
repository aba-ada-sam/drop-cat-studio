"""RECIPE.json parity: the recipe file must MATCH the live code constants
exactly, or one of them is lying about being the source of truth.

NO GPU, no app boot. RUN IT WITH:  python tests/test_recipe.py
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


from features.song_video import recipe as recipe_mod  # noqa: E402
from features.song_video import sing_grid, tiers, window_energy  # noqa: E402
from features.song_video.artifact_screens import (  # noqa: E402
    RIBBON_MAX_INFESTED, RIBBON_P95_CLEAN, RIBBON_P95_INFESTED,
)
from features.song_video.pipeline import (  # noqa: E402
    SYNC_ENFORCE, SYNC_RANK_FLOOR,
)

print("\n-- A: the file loads and validates --")
r = recipe_mod.load()
ok(True, "RECIPE.json loads clean through the validating loader")

print("\n-- B: render values match live code --")
ok(r["render"]["fps"] == sing_grid.SING_FPS, "fps matches sing_grid")
ok(r["frames"]["grip_ceiling"] == sing_grid.SING_MAX_FRAMES,
   "grip ceiling matches SING_MAX_FRAMES")
ok(r["frames"]["content_max"] == sing_grid.SING_CONTENT_MAX_FRAMES,
   "content budget matches SING_CONTENT_MAX_FRAMES")
ok(abs(r["assembly"]["crossfade_s"] - sing_grid.XFADE_S) < 1e-9,
   "crossfade matches sing_grid.XFADE_S")
ok(abs(r["assembly"]["head_trim_s"] - sing_grid.HEAD_TRIM_S) < 1e-9
   and abs(r["assembly"]["tail_trim_s"] - sing_grid.TAIL_TRIM_S) < 1e-9,
   "head/tail trims match sing_grid")

print("\n-- C: acceptance values match live code --")
ok(abs(r["acceptance"]["sync_rank_floor"] - SYNC_RANK_FLOOR) < 1e-9,
   "sync floor matches pipeline.SYNC_RANK_FLOOR")
ok(r["acceptance"]["sync_enforce"] == SYNC_ENFORCE,
   "sync_enforce state matches pipeline.SYNC_ENFORCE -- re-arming in code "
   "without updating the recipe (or vice versa) fails HERE")
rg = r["acceptance"]["ribbon_gate"]
ok(abs(rg["p95_clean"] - RIBBON_P95_CLEAN) < 1e-9
   and abs(rg["p95_infested"] - RIBBON_P95_INFESTED) < 1e-9
   and abs(rg["abs_max"] - RIBBON_MAX_INFESTED) < 1e-9,
   "ribbon gate three-band thresholds match artifact_screens")

print("\n-- D: tier shape matches tiers.py --")
admin = tiers.tier("admin")
user = tiers.tier("user")
ok(r["tiers"]["format_s"] == admin["target_s"] == user["target_s"],
   "30s format matches both tiers (the 22:55 ruling)")
ok(r["tiers"]["clip_s"] == admin["clip_s"]
   and r["tiers"]["num_clips"] == admin["num_clips"],
   "clip shape matches")
ok(r["tiers"]["admin_best_of_n"] == admin["best_of_n"]
   and r["tiers"]["user_best_of_n"] == user["best_of_n"],
   "take budgets match")

print("\n-- E: energy floors match window_energy --")
ok(abs(-40.0 - window_energy.VOICED_FLOOR_DBFS) < 1e-9
   and abs(0.20 - window_energy.VOICED_FRAC_FLOOR) < 1e-9,
   "voiced floor constants unchanged from the validated values")

print("\n-- E2: v3 multi-scene contract present and coherent --")
ms = r["production"].get("multi_scene") or {}
ok(all(k in ms for k in ("flags", "anchor_build", "tail_stub_rule", "scene_prompt_rule")),
   "multi_scene block carries all four contract keys")
ok("2.5" in ms.get("tail_stub_rule", ""),
   "tail-stub threshold documented (chain.py uses 2.5s)")
ok("TRANSPLANT" in ms.get("anchor_build", "").upper(),
   "anchor rule stays transplant-first (PRIME RULE)")
ok("PER-SCENE" in r["production"].get("dof_finish", "").upper(),
   "dof_finish records the per-scene mask rule")
ok("150" in str(r["conditioning_audio"].get("highpass_hz", "")),
   "highpass stays 150 (Andrew's oon closure ruling)")

print("\n-- F: a corrupted recipe fails loudly --")
import json, tempfile  # noqa: E402
bad = dict(r)
bad["frames"] = dict(r["frames"], grip_ceiling=250)   # not 8k+1
tmp = tempfile.mktemp(suffix=".json")
json.dump(bad, open(tmp, "w"))
try:
    recipe_mod.load(tmp)
    ok(False, "a non-8k+1 ceiling must be rejected")
except recipe_mod.RecipeError:
    ok(True, "non-8k+1 ceiling rejected by the loader")
finally:
    os.unlink(tmp)

print("\n" + (f"ALL {PASS} PASS" if FAIL == 0 else f"{FAIL} FAIL / {PASS + FAIL} total"))
sys.exit(0 if FAIL == 0 else 1)
