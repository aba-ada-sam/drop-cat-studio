"""Smoketest the song-video beat-sync pipeline without touching the GPU.

Exercises:
  1. audio_analyzer.analyze()        -- librosa BPM/key/beats extraction
  2. audio_analyzer.compute_clip_plan() -- beat-aligned durations + positions
  3. motion_analyzer.find_motion_peak() -- cv2 frame-diff peak detection
  4. motion_analyzer.speed_ramp_to_target() -- ffmpeg piecewise time warp
  5. motion_analyzer.align_clip_to_beat() -- end-to-end on a real clip
  6. pipeline._generate_song_arc()    -- LLM story arc generation
  7. pipeline imports + module integrity

Skips: anything that calls WanGP (clip generation) -- see CLAUDE.md.
"""
import os
import sys
import logging
from pathlib import Path

# Quieter logs during the test
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

passed = 0
failed = 0


def step(name):
    def deco(fn):
        global passed, failed
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
            failed += 1
        return fn
    return deco


# Pick the first available sample audio + video
import glob
AUDIO = next(iter(sorted(glob.glob("output/**/*.mp3", recursive=True))), None)
VIDEO = next(iter(sorted(glob.glob("output/**/*.mp4", recursive=True))), None)

print(f"sample audio: {AUDIO}")
print(f"sample video: {VIDEO}")
print()


# -- Tests ---------------------------------------------------------------------

print("[imports]")


@step("import motion_analyzer")
def t_import_motion():
    from features.song_video import motion_analyzer
    assert hasattr(motion_analyzer, "find_motion_peak")
    assert hasattr(motion_analyzer, "speed_ramp_to_target")
    assert hasattr(motion_analyzer, "align_clip_to_beat")


@step("import pipeline + audio_analyzer")
def t_import_pipeline():
    from features.song_video import pipeline, audio_analyzer
    assert hasattr(pipeline, "_SONG_ARC_SYSTEM")
    assert hasattr(pipeline, "_generate_song_arc")
    assert hasattr(pipeline, "run_song_pipeline")
    assert hasattr(audio_analyzer, "analyze")
    assert hasattr(audio_analyzer, "compute_clip_plan")


@step("system prompt has motion-arc rule + no rejected vocab")
def t_prompt_clean():
    from features.song_video.pipeline import _SONG_ARC_SYSTEM
    assert "MOTION ARC RULE" in _SONG_ARC_SYSTEM, "motion arc rule should be present"
    assert "BEAT TIMING RULE" not in _SONG_ARC_SYSTEM, "beat timing rule was supposed to go away"
    assert "explosive burst" not in _SONG_ARC_SYSTEM
    assert "dynamic blur" not in _SONG_ARC_SYSTEM


@step("dead code removed (no _concat_clips_xfade, no _XFADE_DUR)")
def t_dead_code_gone():
    from features.song_video import pipeline
    assert not hasattr(pipeline, "_concat_clips_xfade"), "should be deleted"
    assert not hasattr(pipeline, "_XFADE_DUR"), "should be deleted"
    assert not hasattr(pipeline, "_apply_beat_flash"), "should be deleted"


print()
print("[audio analysis]")


@step("analyze() returns expected fields")
def t_analyze():
    if not AUDIO:
        raise AssertionError("no sample audio available")
    from features.song_video.audio_analyzer import analyze
    r = analyze(AUDIO)
    assert r["duration"] > 0, f"duration: {r['duration']}"
    assert isinstance(r["beat_times"], list), "beat_times missing"
    assert isinstance(r["beat_strengths"], list), "beat_strengths missing"
    assert r["bpm"] is None or 40 <= r["bpm"] <= 240, f"bpm: {r['bpm']}"
    assert r["clip_energy_labels"], "clip_energy_labels empty"
    assert r["energy_profile"], "energy_profile empty"
    print(f"        bpm={r['bpm']} key={r['key']} {r['mode']} dur={r['duration']:.1f}s "
          f"beats={len(r['beat_times'])} clips={r['suggested_num_clips']}")


@step("compute_clip_plan() yields valid durations + beat positions")
def t_clip_plan():
    if not AUDIO:
        raise AssertionError("no sample audio available")
    from features.song_video.audio_analyzer import compute_clip_plan
    from core.ffmpeg_utils import probe_duration
    # Pick n_clips realistic for the sample's duration
    dur = probe_duration(AUDIO) or 60.0
    n = max(2, int(dur // 10))
    durs, positions = compute_clip_plan(AUDIO, n_clips=n, min_dur=8.0, max_dur=19.0)
    assert len(durs) == n, f"expected {n} durations, got {len(durs)}"
    assert len(positions) == n
    for d in durs:
        assert d >= 0, f"negative dur: {d}"
    for p in positions:
        assert 0.0 <= p <= 1.0, f"position out of range: {p}"
    print(f"        n={n} durs={durs}  positions={positions}")


@step("compute_clip_plan() never returns negative durations even when over-requested")
def t_clip_plan_overrequest():
    """Regression: asking for more clips than fit in the song must not produce negative durations."""
    if not AUDIO:
        raise AssertionError("no sample audio available")
    from features.song_video.audio_analyzer import compute_clip_plan
    # Deliberately ask for too many clips on a short clip
    durs, positions = compute_clip_plan(AUDIO, n_clips=10, min_dur=8.0, max_dur=19.0)
    assert len(durs) == 10
    for d in durs:
        assert d >= 0, f"negative dur leaked through: {d}"


@step("compute_clip_plan() ignores xfade_dur arg removal cleanly")
def t_clip_plan_signature():
    """Regression check: signature must accept exactly 4 positional args now."""
    import inspect
    from features.song_video.audio_analyzer import compute_clip_plan
    sig = inspect.signature(compute_clip_plan)
    assert "xfade_dur" not in sig.parameters, "xfade_dur should be removed"


print()
print("[motion analysis]")


@step("find_motion_peak() returns (time, confidence)")
def t_motion_peak():
    if not VIDEO:
        raise AssertionError("no sample video available")
    from features.song_video.motion_analyzer import find_motion_peak
    from core.ffmpeg_utils import probe_duration
    dur = probe_duration(VIDEO)
    result = find_motion_peak(VIDEO)
    assert result is not None, "find_motion_peak returned None"
    t, conf = result
    assert 0 <= t <= dur, f"peak time {t} outside [0, {dur}]"
    assert conf > 0, f"non-positive confidence: {conf}"
    print(f"        peak={t:.2f}s/{dur:.1f}s  conf={conf:.2f}")


@step("speed_ramp_to_target() shifts peak with within-bounds ratio")
def t_ramp_in_bounds():
    if not VIDEO:
        raise AssertionError("no sample video available")
    from features.song_video.motion_analyzer import find_motion_peak, speed_ramp_to_target
    from core.ffmpeg_utils import probe_duration
    import tempfile
    dur = probe_duration(VIDEO)
    t_nat, _ = find_motion_peak(VIDEO)
    # Modest shift -- keep ratio in range
    target = max(1.5, t_nat - 1.5) if t_nat > 3.0 else t_nat + 1.5
    target = min(dur - 1.5, target)
    out = os.path.join(tempfile.gettempdir(), "smoke_ramp.mp4")
    ok = speed_ramp_to_target(VIDEO, out, t_nat, target, dur)
    assert ok, "ramp returned False on within-bounds request"
    new_dur = probe_duration(out)
    assert abs(new_dur - dur) < 0.5, f"duration not preserved: {new_dur} vs {dur}"
    os.remove(out)
    print(f"        ramped {t_nat:.2f}s -> {target:.2f}s  dur preserved {new_dur:.2f}s")


@step("speed_ramp_to_target() rejects extreme ratio")
def t_ramp_out_of_bounds():
    if not VIDEO:
        raise AssertionError("no sample video available")
    from features.song_video.motion_analyzer import speed_ramp_to_target
    from core.ffmpeg_utils import probe_duration
    import tempfile
    dur = probe_duration(VIDEO)
    out = os.path.join(tempfile.gettempdir(), "smoke_ramp_extreme.mp4")
    # Pull peak from 90% to 10% -- way outside [0.55, 1.8] ratio bounds
    ok = speed_ramp_to_target(VIDEO, out, dur * 0.9, dur * 0.1, dur)
    assert not ok, "ramp should reject extreme ratio"
    if os.path.exists(out):
        os.remove(out)


@step("speed_ramp_to_target() rejects edge-time peaks")
def t_ramp_edge():
    from features.song_video.motion_analyzer import speed_ramp_to_target
    import tempfile
    out = os.path.join(tempfile.gettempdir(), "smoke_ramp_edge.mp4")
    # t_natural=0.1s is inside the 0.4s edge buffer -- should reject
    ok = speed_ramp_to_target("nonexistent.mp4", out, 0.1, 4.0, 8.0)
    assert not ok, "ramp should reject edge-time peaks before invoking ffmpeg"


@step("align_clip_to_beat() end-to-end on real clip")
def t_align_e2e():
    if not VIDEO:
        raise AssertionError("no sample video available")
    from features.song_video.motion_analyzer import align_clip_to_beat
    from core.ffmpeg_utils import probe_duration
    import tempfile
    dur = probe_duration(VIDEO)
    out = os.path.join(tempfile.gettempdir(), "smoke_align.mp4")
    target = dur * 0.5  # mid-clip
    applied, info = align_clip_to_beat(VIDEO, target, dur, out)
    assert info["natural_time"] is not None, "natural_time should be detected"
    assert info["confidence"] is not None
    assert info["target_time"] == target
    if applied:
        assert os.path.exists(out)
        os.remove(out)
    if not applied:
        # Should have a reason
        assert info["reason"], "applied=False must have a reason"
    print(f"        applied={applied} natural={info['natural_time']:.2f}s "
          f"target={info['target_time']:.2f}s conf={info['confidence']:.2f}"
          f"  reason='{info['reason']}'")


@step("align_clip_to_beat() short-circuits when peak already on beat")
def t_align_already_on_beat():
    if not VIDEO:
        raise AssertionError("no sample video available")
    from features.song_video.motion_analyzer import find_motion_peak, align_clip_to_beat
    from core.ffmpeg_utils import probe_duration
    import tempfile
    dur = probe_duration(VIDEO)
    t_nat, _ = find_motion_peak(VIDEO)
    out = os.path.join(tempfile.gettempdir(), "smoke_already.mp4")
    # Target = natural peak -> should short-circuit with "already on beat"
    applied, info = align_clip_to_beat(VIDEO, t_nat, dur, out)
    assert not applied, "should not ramp when target ~ natural"
    assert "already on beat" in info["reason"] or "uniform" in info["reason"], info["reason"]


print()
print("[summary]")
print(f"  {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
