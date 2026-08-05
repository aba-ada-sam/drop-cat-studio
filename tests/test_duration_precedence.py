"""FIX 3 (2026-08-05, ROLLBACK_MAP findings a+c): the arc must not override
the tier's clip duration, and delivery must target the rendered window, not
the full song. NO GPU, NO ffmpeg, NO renders.

Two independent things tested:
  1. _resolve_clip_duration (import-light): requested clip_duration always
     wins over the LLM story-arc's per-clip "duration", real function calls.
  2. Static source-text + signature checks that the merge path
     (_merge_video_audio_trim) honors window_delivery -- per the task's own
     instruction to make this one a static check rather than a live-render
     one (which would need real ffmpeg fixtures and video/audio files).

RUN IT WITH:  python tests/test_duration_precedence.py
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import features.song_video.pipeline as pipeline  # noqa: E402
from features.song_video.pipeline import _resolve_clip_duration  # noqa: E402

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


print("\n-- A: _resolve_clip_duration -- requested clip_duration ALWAYS wins, "
     "the arc's 'duration' never overrides it --")
clip_durations = [6.0, 6.0, 6.0, 6.0, 6.0]

ok(_resolve_clip_duration({"duration": 9.0}, clip_durations, 0, 6.0) == 6.0,
   "arc asks for 9.0s on clip 0 -> still 6.0s (the request), not 9.0s")
ok(_resolve_clip_duration({"duration": 4.0}, clip_durations, 2, 6.0) == 6.0,
   "arc asks for LESS (4.0s) on clip 2 -> still 6.0s, direction doesn't matter")
ok(_resolve_clip_duration({"duration": "11.5"}, clip_durations, 1, 6.0) == 6.0,
   "arc duration as a string -> still coerced-and-ignored, request wins")
ok(_resolve_clip_duration({}, clip_durations, 0, 6.0) == 6.0,
   "arc entry has no 'duration' key at all -> falls through to the request cleanly")
ok(_resolve_clip_duration("just a prompt string", clip_durations, 0, 6.0) == 6.0,
   "arc entry is a bare string (not a dict) -> isinstance guard holds, request wins")
ok(_resolve_clip_duration({"duration": None}, clip_durations, 0, 6.0) == 6.0,
   "arc duration explicitly None -> request wins, no crash")
ok(_resolve_clip_duration({"duration": "not-a-number"}, clip_durations, 0, 6.0) == 6.0,
   "arc duration is garbage text -> caught, does not raise, request wins")
ok(_resolve_clip_duration({"duration": 6.0}, clip_durations, 0, 6.0) == 6.0,
   "arc duration happens to EQUAL the request -> still 6.0 (no mismatch to log, "
   "same result either way)")

print("\n-- B: index fallback and clamping still work exactly as before --")
ok(_resolve_clip_duration({"duration": 20.0}, clip_durations, 9, 7.5) == 7.5,
   "i out-of-range for clip_durations -> falls back to clip_dur (7.5), arc still ignored")
ok(_resolve_clip_duration({"duration": 3.0}, [2.0], 0, 6.0) == 4.0,
   "requested 2.0s clamps up to the 4.0s floor (arc's 3.0 is irrelevant either way)")
ok(_resolve_clip_duration({"duration": 3.0}, [15.0], 0, 6.0) == 12.0,
   "requested 15.0s clamps down to the 12.0s ceiling")

print("\n-- C: this is a REAL fix, not a no-op -- proves the OLD formula would "
     "have used the arc's value where the NEW one does not --")


def _old_this_dur(arc_entry, clip_durations, i, clip_dur):
    """The exact pre-fix formula (8ba2df2, 2026-05-24), preserved so this
    comparison stays meaningful after the live code has moved on."""
    arc_dur = arc_entry.get("duration") if isinstance(arc_entry, dict) else None
    this_dur = float(arc_dur) if arc_dur else (clip_durations[i] if i < len(clip_durations) else clip_dur)
    return max(4.0, min(12.0, this_dur))


arc_entry = {"duration": 10.0}
old = _old_this_dur(arc_entry, clip_durations, 0, 6.0)
new = _resolve_clip_duration(arc_entry, clip_durations, 0, 6.0)
ok(old == 10.0, f"sanity: the OLD formula really did honor the arc (got {old})")
ok(new == 6.0, f"the NEW function ignores it and renders the requested 6.0s (got {new})")
ok(old != new, "OLD and NEW disagree on this exact input -- proves this is a real "
               "behavior change, not a refactor that happens to return the same numbers")

print("\n-- D: static check -- pipeline.py's this_dur assignment actually calls "
     "_resolve_clip_duration (source text) --")
_pipeline_path = os.path.join(os.path.dirname(__file__), "..", "features",
                              "song_video", "pipeline.py")
psrc = open(_pipeline_path, encoding="utf-8").read()
ok("this_dur  = _resolve_clip_duration(_arc_entry, clip_durations, i, clip_dur, clip_num=clip_num)"
   in psrc,
   "the per-clip loop assigns this_dur from _resolve_clip_duration, not an inline "
   "arc-first computation")
# The old inline pattern (`float(_arc_dur) if _arc_dur else ...`) must be GONE from
# the live assignment, not just present somewhere else in a docstring/comment.
ok('float(_arc_dur) if _arc_dur else' not in psrc,
   "the old arc-first ternary is gone from the live code entirely")

print("\n-- E: static + signature checks -- the merge path honors window_delivery --")
ok("def _merge_video_audio_trim(" in psrc, "_merge_video_audio_trim still exists")

sig = inspect.signature(pipeline._merge_video_audio_trim)
ok("window_delivery" in sig.parameters and "first_clip_start" in sig.parameters,
   f"_merge_video_audio_trim's real signature carries both new params: {list(sig.parameters)}")
ok(sig.parameters["window_delivery"].default is False,
   "window_delivery defaults False -- every existing full-song caller is unaffected")
ok(sig.parameters["first_clip_start"].default == 0.0,
   "first_clip_start defaults 0.0 -- harmless when window_delivery is off")

_call_marker = "merged     = _merge_video_audio_trim("
ok(_call_marker in psrc, "the known call site text still exists (anchor)")
_call_idx = psrc.find(_call_marker)
_call_window = psrc[_call_idx:_call_idx + 400]
ok('window_delivery=bool(settings.get("window_delivery", False))' in _call_window,
   "the call site reads window_delivery from the JOB'S settings, not a hardcoded value")
ok("first_clip_start=(_clip_start_times[0] if _clip_start_times else pad_before)"
   in _call_window,
   "the call site passes the corrected clip-0 start time as first_clip_start "
   "(falls back to pad_before if start times are unavailable)")

_fn_idx = psrc.find("def _merge_video_audio_trim(")
_fn_src = psrc[_fn_idx:_fn_idx + 6000]
_wd_idx = _fn_src.find("if window_delivery:")
# Bounded at the FIRST line of the old (non-windowed) path, right after the
# window_delivery branch's own `return None` -- NOT at `fill = "loop"`, which
# sits much further down past the old path's "three fill modes" comment block.
# That comment mentions -stream_loop as HISTORY (documenting the pre-f9fef20
# behaviour); a looser boundary would sweep it into "the branch" and produce a
# false failure on a substring match that was never inside the branch's own
# code to begin with.
_old_path_idx = _fn_src.find("true_audio_dur = probe_duration(audio_path)")
ok(_wd_idx != -1 and _old_path_idx != -1 and _wd_idx < _old_path_idx,
   "the window_delivery branch is checked BEFORE the full-song loop-fill logic")
_wd_branch = _fn_src[_wd_idx:_old_path_idx]
ok(_wd_branch.count("return out_path") == 1 and _wd_branch.count("return None") == 1,
   "the window_delivery branch always returns on its own -- it can never fall "
   "through into the full-song trim/freeze/loop logic below it")
ok("-stream_loop" not in _wd_branch,
   "the window_delivery branch's ffmpeg command never loops video -- literally "
   "absent, not just unreached")
ok("never looped to fill it" in _wd_branch,
   "the branch's own log line states the no-loop guarantee")
# And the reverse sanity: -stream_loop DOES still exist further down, in the
# untouched old path -- proving the absence above is because the branch is
# clean, not because the marker vanished from the file entirely.
ok("-stream_loop" in _fn_src[_old_path_idx:],
   "sanity: -stream_loop still exists in the OLD (non-windowed) loop-fill path, "
   "unchanged -- full-song callers keep their existing behavior")

print("\n-- F: tiers.py's job_payload carries window_delivery=True (cross-check; "
     "test_tiers.py Group F owns the authoritative assertion on this) --")
from features.song_video import tiers  # noqa: E402
p = tiers.job_payload("user", "song.wav", "photo.png")
ok(p.get("window_delivery") is True,
   "job_payload() sets window_delivery=True for the tier product")

print("\n" + (f"ALL {PASS} PASS" if FAIL == 0 else f"{FAIL} FAIL / {PASS + FAIL} total"))
sys.exit(0 if FAIL == 0 else 1)
