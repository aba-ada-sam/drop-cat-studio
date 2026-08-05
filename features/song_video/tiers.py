"""Product tiers for sing videos: 30 seconds for everyone.

Andrew, 2026-08-04 (~22:55, superseding his own 60s-admin spec from ~21:00 the
same night): "why are we still doing 60s when 30s is good enough?" -- so 30s IS
the format, for admin and users both. The tiers still differ, but only in TAKE
BUDGET now, not length. This module is the ONE place those shapes are defined,
so a tier change is a constant edit rather than a hunt through the route, the
pipeline and whatever script last hard-coded a clip count.

WHY THE CLIP LENGTH IS WHAT IT IS. Clip duration is not a free knob:
  * LTX-2 frame counts are 8k+1, and audio conditioning stops gripping past
    ~250 frames (measured 4/4 synced at 249f vs 1/21 at 465-473f). At 24fps
    that is a hard ceiling of ~10.3s of rendered material per take.
  * Every clip loses HEAD+TAIL trim before assembly, and each junction eats a
    crossfade, so the RENDER is longer than the slot it fills (see sing_grid:
    headroom-then-trim).
So a tier picks a clip length that divides its target cleanly AND leaves the
render under the ceiling. sing_grid.frames_for_slot does the arithmetic; the
assertions at import time make a bad tier fail loudly here rather than silently
producing unsyncable clips.

TAKE BUDGET. With SYNC-OR-DIE (pipeline.SyncFloorNotMet) a voiced window that
never clears the sync floor FAILS rather than banking a statue, so best_of_n is
what buys a usable clip on a hard window rather than a nice-to-have. Admin gets
a deeper budget than users because an admin render is supervised and a user
render is not.
"""
from __future__ import annotations

from features.song_video.sing_grid import (
    SING_FPS, SING_MAX_FRAMES, exact_slot_frames, frames_for_slot,
)

TIERS: dict[str, dict] = {
    # Admin: same 30s length as users -- the difference is the take budget.
    # A supervised render can afford more GPU per hard window than an
    # unattended one; it should not be a longer video, per Andrew's 22:55 call.
    "admin": {
        "target_s": 30.0,
        "clip_s": 6.0,        # 5 clips, 153f render each -- well under the ceiling
        "best_of_n": 6,
        "label": "30s admin",
    },
    # User tier: shallower budget so an unsupervised job cannot spend an
    # admin-sized amount of GPU on one hard window.
    "user": {
        "target_s": 30.0,
        "clip_s": 6.0,        # 5 clips
        "best_of_n": 3,
        "label": "30s user",
    },
}


def tier(name: str) -> dict:
    """Resolved tier config: clip count, per-clip length, take budget."""
    if name not in TIERS:
        raise KeyError(f"unknown tier {name!r}; known: {sorted(TIERS)}")
    t = dict(TIERS[name])
    t["num_clips"] = int(round(t["target_s"] / t["clip_s"]))
    t["render_frames"] = frames_for_slot(t["clip_s"], with_junction=True)
    t["slot_frames"] = exact_slot_frames(t["clip_s"])
    t["total_frames"] = t["slot_frames"] * t["num_clips"]
    return t


def job_payload(name: str, audio_path: str, photo_path: str) -> dict:
    """The exact body to POST to /api/song-video/generate for this tier.

    lip_sync is set EXPLICITLY true: the route defaults it to False, and with it
    off none of the conditioning path runs at all -- no isolation, no window
    energy, no screens, no best-of-N. A tier that means "singing video" has to
    say so, and the explicit flag also selects the hard-fail branch (an
    isolation failure stops the job instead of quietly rendering a mute clip).
    """
    t = tier(name)
    return {
        "audio_path": audio_path,
        "photo_path": photo_path,
        "lip_sync": True,
        "best_of_n": t["best_of_n"],
        "clip_duration": int(t["clip_s"]),
        "num_clips": t["num_clips"],
    }


# Fail at import if a tier would render past the conditioning-grip ceiling --
# that failure is SILENT at runtime (the clip renders fine and simply does not
# lip-sync), so it must be caught here where it is loud.
for _n, _cfg in TIERS.items():
    _f = frames_for_slot(_cfg["clip_s"], with_junction=True)
    assert _f <= SING_MAX_FRAMES, (
        f"tier {_n!r}: a {_cfg['clip_s']}s clip renders {_f} frames, past the "
        f"{SING_MAX_FRAMES}-frame audio-conditioning ceiling -- it would render "
        f"cleanly and never lip-sync")
    assert abs(_cfg["target_s"] / _cfg["clip_s"] - round(_cfg["target_s"] / _cfg["clip_s"])) < 1e-9, (
        f"tier {_n!r}: {_cfg['target_s']}s does not divide into whole "
        f"{_cfg['clip_s']}s clips, so the video would over- or under-run")
