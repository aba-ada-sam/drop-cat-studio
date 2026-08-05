"""Loader for RECIPE.json -- the shared lip-sync recipe of record.

2026-08-04: two codebases (DCS features/song_video and DCMVS chain.py) carried
the same recipe as separately-maintained constants, and they drifted -- fps 25
vs 24, crossfade 0.25 vs 0.12, a frame cap missing entirely on one side. Each
drift was invisible until a render paid for it. RECIPE.json is the one file
both are meant to read; this module loads and sanity-checks it.

CURRENT SCOPE, deliberate: DCS code does NOT yet consume these values at
runtime -- the constants in sing_grid.py / tiers.py / pipeline.py remain the
live source, and tests/test_recipe.py asserts the recipe file MATCHES them
exactly. That makes the file trustworthy-by-test before anything depends on
it; flipping consumption to read from here is a later, separate change (same
staging discipline for DCMVS -- see the file's own dcmvs section).
"""
from __future__ import annotations

import json
import os

RECIPE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "RECIPE.json")


class RecipeError(RuntimeError):
    """The recipe file is missing, unparseable, or violates its own invariants."""


def load(path: str = RECIPE_PATH) -> dict:
    """Load + validate RECIPE.json. Raises RecipeError on any violation --
    a half-valid recipe silently defaulting is exactly the drift this exists
    to end."""
    if not os.path.isfile(path):
        raise RecipeError(f"recipe file missing: {path}")
    try:
        r = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        raise RecipeError(f"recipe unparseable: {e}") from e

    for key in ("model", "render", "frames", "assembly", "conditioning_audio",
                "acceptance", "tiers", "per_device"):
        if key not in r:
            raise RecipeError(f"recipe missing section {key!r}")

    fr = r["frames"]
    # 8k+1 sanity: the ceiling and the content budget must themselves sit on
    # the grid the rule describes, or the file contradicts itself.
    if (fr["grip_ceiling"] - 1) % 8 != 0:
        raise RecipeError(f"grip_ceiling {fr['grip_ceiling']} is not 8k+1")
    if fr["content_max"] >= fr["grip_ceiling"]:
        raise RecipeError("content_max must leave headroom under grip_ceiling")

    t = r["tiers"]
    n = t["format_s"] / t["clip_s"]
    if abs(n - round(n)) > 1e-9 or int(round(n)) != t["num_clips"]:
        raise RecipeError(
            f"tiers self-inconsistent: {t['format_s']}s / {t['clip_s']}s "
            f"!= {t['num_clips']} clips")

    acc = r["acceptance"]
    if acc["sync_enforce"] and "recalibrat" not in acc.get("sync_enforce_note", ""):
        # Guard the ledger rule mechanically: re-arming requires the note to
        # carry its recalibration story, not just a flipped boolean.
        raise RecipeError(
            "sync_enforce=true but sync_enforce_note carries no recalibration "
            "story -- the floor may not be re-armed by a lone flag flip")

    if not (0 < acc["sync_rank_floor"] < 1):
        raise RecipeError("sync_rank_floor out of range")

    rd = r["render"]
    if rd["fps"] != 24:
        raise RecipeError(
            f"fps {rd['fps']} != 24 -- WanGP's ltx2_19B arch spec is 24; 25 was "
            f"the documented 4% a/v drift bug, do not resurrect it")

    return r
