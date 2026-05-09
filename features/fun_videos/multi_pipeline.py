"""Multi-clip story pipeline: photo -> N chained video clips -> concat -> audio -> merge.

Each clip uses the last frame of the previous clip as its start image, enforcing
visual continuity across the full sequence.  Audio is a single ACE-Step pass over
the concatenated video — never per-clip — so the music has a natural arc.
"""
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from core.ffmpeg_utils import probe_duration
from core.llm_client import TIER_BALANCED, encode_image_b64, parse_json_response
from features.fun_videos.pipeline import _prep_photo, _finalize_prompt, _sample_music_frames

log = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"

_FALLBACK_PHASES = [
    "erupts into full motion, kinetic energy explodes outward",
    "surges forward with raw power, movement intensifies",
    "tears through space, momentum builds",
    "launches into dramatic action, force ripples outward",
    "crashes through with unstoppable drive",
    "pulls back, revealing scale of movement",
    "slams into peak intensity, motion at full power",
    "reaches final explosive beat, energy released",
]


# ── Story arc generation ──────────────────────────────────────────────────────

# Two system prompts -- LTX models are intolerant of aggressive verbs (the
# distilled schedule has so few denoising steps that "explosive" prompts get
# read as "replace the scene with stock fire/lightning/anime imagery"), so
# they get a gentle scene-preserving variant. Wan I2V keeps the kinetic one.

_STORY_ARC_BASE = """\
You are planning a multi-clip short film from a still photograph.
Each clip starts from the last frame of the previous, so prompts must chain visually.

STEP 1 -- READ THE PHOTO CAREFULLY. Note every visible element:
- Subject markers (face, hair, clothing, skin tone, mech parts, fur, etc.)
- Setting / location (room, landscape, weather, time of day)
- Background and props (furniture, architecture, foliage, vehicles, objects)
- Visual style cues (photorealistic, painted, anime, cinematic film stock, etc.)

Decide which scene type it is:

  TYPE A -- PEOPLE / CHARACTERS are the main subject (portrait, figure, face,
    person, character, mech, creature):
    - Identify their specific visual markers: hair color/style, clothing color/type, skin tone
    - EVERY prompt must name these features: "red-haired woman in blue jacket..."
    - Without this the video model generates a different person each clip

  TYPE B -- LANDSCAPE / ARCHITECTURE / NO PEOPLE (buildings, nature, seascape, cityscape):
    - DO NOT invent characters, soldiers, people, or figures that are not in the photo
    - Animate what is ALREADY IN THE SCENE: sea waves, clouds, flags, fire, smoke, wind,
      light raking across surfaces, weather rolling in, water surging, trees thrashing
    - The camera can react to these forces but does not invent new subjects

CRITICAL: SCENE PRESERVATION ACROSS CLIPS. Each clip is a separate generation
that hallucinates aggressively if you only describe motion. Every prompt MUST
re-state the FULL VISUAL CONTEXT so the model holds the scene:
  - Restate the subject + their visual markers
  - Restate the setting / location (e.g. "in the same wood-panel room with
    mushroom forest visible through the window, laptop on the desk")
  - Restate the visual style (e.g. "photorealistic", "cinematic 35mm",
    "painted illustration") so the look does not drift into generic
    "fire / electric / anime" defaults
This anchoring is mandatory for clips 2 onward; without it the model
progressively replaces the original scene with motion-themed hallucinations.

Common rules:
- Describe only what is PHYSICALLY VISIBLE: bodies, surfaces, weather, objects in motion
- Every action must be a real physical thing a camera could capture -- no abstract concepts
- NO camera moves as the primary event (camera reacts, never leads)
- BANNED words/phrases: "establishes", "reveals", "opens on", "we see", "the camera",
  "snaps to", "formation", "attention", "unfolds", "transforms", "becomes", "reveals"
- BANNED: inventing subjects not in the photo (no new animals, people, or objects)
- BANNED: dropping the setting / props / background after the first clip
- Return ONLY valid JSON: {"clips": [{"prompt": "prompt1", "duration": 5}, {"prompt": "prompt2", "duration": 8}]}
- duration is seconds per clip (integer 4-15). Vary durations for pacing.
- Total durations should sum close to the target_total_seconds given in the prompt.\
"""

_STORY_ARC_KINETIC = _STORY_ARC_BASE + """

ENERGY: kinetic action. Each prompt 45-65 words, opens with a strong action verb
(erupts, slams, surges, thrashes). Arc: intense opening -> escalation -> peak.
Use this style only with Wan I2V models, which preserve identity through
aggressive prompts. action/impact = 4-6s, sustained drama = 7-10s, final reveal = 6-12s.\
"""

_STORY_ARC_GENTLE = _STORY_ARC_BASE + """

ENERGY: ambient and micro-motion only. Each clip is the SAME framed shot
with small environmental and gestural movement -- never a new camera angle,
never the subject locomoting across the frame, never a close-up push-in.
LTX-2 Distilled at 8 steps amplifies any explicit action verb into a
camera move plus scale drift: "walks" becomes a tracking shot, "kneels"
becomes a close-up reframe, by clip 4 the model has invented a different
character. The only safe defaults are tiny gestures and environmental
motion (wind, light, leaves, smoke, water).

CAMERA LOCK (mandatory in EVERY prompt):
- Camera is locked off, completely still, exact same framing, same focal
  length, same distance to subject as the source photo.
- No push-in, no pull-out, no pan, no tilt, no dolly, no zoom.
- The subject DOES NOT walk across the frame -- they stay in the same
  position they occupy in the source photo.

PROMPT SHAPE (mandatory). Each prompt 50-75 words:
  1. CAMERA LOCK CLAUSE (~10 words): "Camera locked off, exact same wide
     framing as the source photo, no movement of the lens."
  2. PRIMARY MOTION (~25-35 words): one or two SMALL motions happening in
     the locked frame. Prefer environmental motion (wind through trees,
     mist drifting, light shifting, smoke rising, water rippling) and
     micro-gestures (head tilt, hand twitch, gentle breath, slow blink,
     fingers shifting on a strap). NEVER include subject locomotion,
     never include another person/object entering the frame.
  3. SCENE ANCHOR (~15-25 words): restate the subject's exact position +
     full setting + visual style: "The miniature figure stays mid-stride
     on the centre yellow road line, same toy-village street, photorealistic
     tilt-shift miniature look."

PREFERRED VERBS (subject): tilts head, blinks slowly, shifts grip, breathes,
stays still, holds the same pose, glances slightly, fingers brush, weight
settles. PREFERRED ENVIRONMENT: drifts, sways, ripples, glints, dims,
brightens, flickers, flutters, settles, eddies, scatters.
BANNED (cause LTX to dramatize): walks, steps, strides, runs, jumps,
turns around, kneels, rises, sweeps, lifts arm, reaches across, exits,
enters, climbs, dives, pivots. Also banned: erupts, slams, explodes,
thrashes, surges, screams, blasts, roars.

ARC: clip 1 establishes the locked frame with one ambient motion. Each
later clip is the SAME locked frame with a DIFFERENT ambient motion or
DIFFERENT micro-gesture (not a different action). Wind direction can
change, leaves can fall vs. swirl, light can dim or brighten, the figure
can blink vs. breathe vs. shift weight. Never escalate -- variety, not
crescendo.

GOOD example progression for "miniature figure with suitcase in toy
village street":
  clip 1: "Camera locked off, exact same wide framing as the source photo,
    no movement of the lens. A gentle breeze moves through the small trees
    lining the street, leaves shift slightly. The miniature figure stays
    mid-stride on the centre yellow road line, suitcase held at the side,
    same toy-village street, photorealistic tilt-shift miniature look."
  clip 2: "Camera locked off, no movement, identical wide composition. The
    small flag on the corner balcony flutters once, then settles. The light
    dims by a hair as a cloud passes overhead. The miniature figure holds
    the same pose, suitcase at side, same toy-village street, photorealistic
    tilt-shift miniature look."
  clip 3: "Camera locked off, identical framing, no lens movement. A few
    leaves fall from the corner tree and drift across the road. The
    figure tilts its head a fraction toward the red building. Same
    miniature toy-village street, photorealistic tilt-shift miniature look."
Each clip = locked frame, different ambient/micro detail, same composition.\
"""


def _system_prompt_for_model(model_name: str) -> str:
    return _STORY_ARC_GENTLE if "ltx" in (model_name or "").lower() else _STORY_ARC_KINETIC


def _generate_story_arc(
    llm_router,
    initial_idea: str,
    n_clips: int,
    photo_path: str | None,
    progress_fn=None,
    target_total_secs: float | None = None,
    default_clip_dur: float = 5.0,
    model_name: str = "",
) -> tuple[list[dict], str]:
    """Generate N sequential motion prompts with per-clip durations.

    Returns (clips, method) where clips is list[{"prompt": str, "duration": float}]
    and method is one of "vision", "text", "fallback".

    Cascade:
      1. Vision call to Ollama (sees the photo, handles NSFW -- never cloud vision)
      2. Text-only call to any provider (uses sanitizer for cloud, user idea as context)
      3. Built-in fallback phases prefixed with user idea (last resort, always works)
    """
    is_ltx = "ltx" in (model_name or "").lower()
    default_idea = (
        "Create a calm observational short film, subtle continuous motion, scene preserved"
        if is_ltx
        else "Create an exciting action-packed short film"
    )
    idea_text = (initial_idea or "").strip() or default_idea
    total_secs = target_total_secs or (n_clips * default_clip_dur)
    user_msg = (
        f"Target video model: {model_name or 'unknown'} "
        f"({'LTX -- use gentle motion only' if is_ltx else 'Wan I2V -- kinetic action OK'})\n"
        f"Initial idea: {idea_text}\n"
        f"Number of clips: {n_clips}\n"
        f"Target total story length: {int(total_secs)}s\n\n"
        f"Generate exactly {n_clips} sequential motion prompts as a story arc.\n\n"
        f"REQUIRED OUTPUT FORMAT -- respond with ONLY this JSON, no other text:\n"
        f'{{"clips": [{{"prompt": "prompt 1", "duration": 5}}, {{"prompt": "prompt 2", "duration": 8}}]}}'
    )
    system_prompt = _system_prompt_for_model(model_name)

    def _parse_clips(text) -> list[dict] | None:
        data = parse_json_response(text)
        if not data:
            return None
        raw = data.get("clips", [])
        if not isinstance(raw, list) or not raw:
            return None
        # Normalise: accept both dict form and legacy plain string form
        result: list[dict] = []
        for item in raw[:n_clips]:
            if isinstance(item, dict):
                prompt = str(item.get("prompt", "")).strip()
                try:
                    dur = max(4.0, min(15.0, float(item.get("duration", default_clip_dur))))
                except (TypeError, ValueError):
                    dur = default_clip_dur
            else:
                prompt = str(item).strip()
                dur = default_clip_dur
            if prompt:
                result.append({"prompt": prompt, "duration": dur})
        if not result:
            return None
        src = len(result)
        while len(result) < n_clips:
            result.append(dict(result[len(result) % src]))
        return result

    # -- Step 1a: Ollama vision (local -- photo stays on-device) --
    frames = []
    if photo_path and os.path.isfile(photo_path):
        b64 = encode_image_b64(photo_path)
        if b64:
            frames = [b64]

    if frames:
        if progress_fn:
            progress_fn("Planning story arc from photo...")
        try:
            text = llm_router.route_vision(
                user_msg, frames,
                tier=TIER_BALANCED, system=system_prompt, max_tokens=1200,
                force_provider="ollama", format_json=True,
            )
            result = _parse_clips(text)
            if result:
                return result, "vision"
            log.warning("[multi] Story arc Ollama vision returned unparseable response -- trying cloud vision")
        except Exception as e:
            log.warning("[multi] Story arc Ollama vision failed (%s) -- trying cloud vision", e)

        # -- Step 1b: Cloud vision fallback (Anthropic/OpenAI) --
        # Ollama's qwen3-vl often outputs thinking blocks instead of JSON; when that
        # happens the story arc must still be grounded in the actual photo content or
        # the video model ignores the source image and hallucinates unrelated scenes.
        try:
            text = llm_router.route_vision(
                user_msg, frames,
                tier=TIER_BALANCED, system=system_prompt, max_tokens=1200,
            )
            result = _parse_clips(text)
            if result:
                return result, "vision"
            log.warning("[multi] Story arc cloud vision returned unparseable response -- trying text-only")
        except Exception as e:
            log.warning("[multi] Story arc cloud vision failed (%s) -- trying text-only", e)

    # -- Step 2: text-only (last resort -- story arc not anchored to photo) --
    if progress_fn:
        progress_fn("Planning story arc (text-only)...")
    try:
        text = llm_router.route(
            [{"role": "user", "content": user_msg}],
            tier=TIER_BALANCED, system=system_prompt, max_tokens=1200,
        )
        result = _parse_clips(text)
        if result:
            return result, "text"
        log.warning("[multi] Story arc text-only returned unparseable response -- using fallback")
    except Exception as e:
        log.warning("[multi] Story arc text-only call failed (%s) -- using fallback", e)

    # -- Step 3: built-in fallback (always works, preserves user idea) --
    base = (initial_idea.strip() + ", ") if initial_idea else ""
    return [
        {"prompt": base + _FALLBACK_PHASES[i % len(_FALLBACK_PHASES)], "duration": default_clip_dur}
        for i in range(n_clips)
    ], "fallback"


_DIRECTOR_SYSTEM = """\
You are a film director reviewing footage from a multi-clip AI-generated short film.
You will be shown 2 frames per clip (early frame and late frame within each clip's segment).
Your job: identify clips that failed so they can be re-shot.

Rate each clip 1-5:
  5 = excellent -- vivid motion, subject consistent with previous clip, story advances
  4 = good -- minor issues but acceptable
  3 = passable -- some problems but tolerable
  2 = weak -- poor motion, subject drifted, or story stalls badly
  1 = bad -- clearly broken, static, or wrong subject

For clips rated <= 2: write an improved motion prompt (35-55 words, action verb first).
Apply the same TYPE rules used for the original:
  TYPE A (people): always name their visual markers (hair, clothing, skin tone)
  TYPE B (landscape/objects): only animate elements already visible -- no invented subjects

Be conservative: flag at most 2-3 clips per pass. Only flag truly broken clips.
Clips rated >= 3 should be kept as-is.

Return ONLY valid JSON, no other text:
{"ratings": [5, 3, 2, 4], "regenerate": [{"clip_idx": 2, "new_prompt": "..."}], "notes": "one sentence what you changed and why"}
"""


def _extract_review_frames(clip_paths: list[str], clip_durations: list[float], job_dir: Path, pass_num: int) -> list[list]:
    """Extract 2 b64-encoded jpg frames per clip, sampled directly from each clip file.

    Samples at 15% and 80% of each clip's own duration. Sampling from individual
    clip files avoids the xfade-overlap problem where assembled-video timestamps
    drift by fade_dur per clip and can land mid-crossfade.
    Returns list of [b64_early, b64_late] per clip; entries may be empty on failure.
    """
    result: list[list] = []
    for i, (path, dur) in enumerate(zip(clip_paths, clip_durations)):
        t1 = max(0.0, dur * 0.15)
        t2 = max(0.0, dur * 0.80)
        frames: list[str] = []
        for t, label in [(t1, "a"), (t2, "b")]:
            frame_path = str(job_dir / f"rv_p{pass_num}_c{i:02d}{label}.jpg")
            r = subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", path,
                 "-vframes", "1", "-q:v", "4", "-vf", "scale=512:-2", frame_path],
                capture_output=True, timeout=30,
            )
            if r.returncode == 0 and Path(frame_path).exists():
                b64 = encode_image_b64(frame_path)
                if b64:
                    frames.append(b64)
            try:
                Path(frame_path).unlink(missing_ok=True)
            except Exception:
                pass
        result.append(frames)
    return result


def _director_analyze(llm_router, frames_per_clip: list[list], story_arc: list, user_idea: str, pass_num: int) -> dict:
    """Send review frames to LLM vision; get per-clip ratings and re-shoot instructions.

    Returns {"ratings": [...], "regenerate": [{"clip_idx": int, "new_prompt": str}], "notes": str}.
    """
    n = len(frames_per_clip)
    clip_descs = []
    for i, clip_data in enumerate(story_arc):
        p = clip_data.get("prompt", "") if isinstance(clip_data, dict) else str(clip_data)
        d = clip_data.get("duration", 5.0) if isinstance(clip_data, dict) else 5.0
        clip_descs.append(f"Clip {i + 1} ({d:.0f}s): {p}")

    user_msg = (
        f"Director pass {pass_num}. Original idea: {user_idea or 'no specific direction'}\n\n"
        f"Story arc ({n} clips):\n" + "\n".join(clip_descs) + "\n\n"
        f"Review each clip. Flag those that need re-shooting (rated 1-2). "
        f"Be conservative -- only flag genuinely broken clips.\n\n"
        f"Required JSON:\n"
        f'{{"ratings": [5,3,2,4], "regenerate": [{{"clip_idx": 2, "new_prompt": "..."}}], "notes": "..."}}'
    )

    all_frames: list[str] = []
    for frames in frames_per_clip:
        all_frames.extend(frames)

    if not all_frames:
        log.warning("[director] Pass %d: no frames extracted -- skipping analysis", pass_num)
        return {"ratings": [], "regenerate": [], "notes": "no frames"}

    try:
        text = llm_router.route_vision(
            user_msg, all_frames,
            tier=TIER_BALANCED, system=_DIRECTOR_SYSTEM, max_tokens=800,
        )
        data = parse_json_response(text)
        if not data:
            log.warning("[director] Pass %d: unparseable LLM response -- no re-shoots", pass_num)
            return {"ratings": [], "regenerate": [], "notes": "parse failed"}

        regen_valid = []
        for r in data.get("regenerate", []):
            if isinstance(r, dict) and "clip_idx" in r and "new_prompt" in r:
                idx = int(r["clip_idx"])
                if 0 <= idx < n:
                    regen_valid.append({"clip_idx": idx, "new_prompt": str(r["new_prompt"]).strip()})

        notes = str(data.get("notes", ""))
        log.info("[director] Pass %d ratings:%s re-shoot:%s -- %s",
                 pass_num, data.get("ratings", []), [r["clip_idx"] for r in regen_valid], notes)
        return {"ratings": data.get("ratings", []), "regenerate": regen_valid, "notes": notes}
    except Exception as e:
        log.warning("[director] Pass %d analysis error: %s", pass_num, e)
        return {"ratings": [], "regenerate": [], "notes": str(e)}


def _run_director_pass(
    job, llm_router,
    clip_paths: list[str], clip_durations: list[float], story_arc: list,
    prepped_photo, assembled_path: str,
    settings: dict, job_dir: Path,
    pass_num: int, pct_start: int, pct_end: int,
    _log, _stopped,
    video_generator, model_name: str, resolution: str,
    ow, oh, steps: int, guidance: float, seed: int,
) -> tuple[list, list, list, str]:
    """Analyze assembled video; re-generate weak clips; re-assemble.

    Returns (clip_paths, clip_durations, story_arc, assembled_path).
    Returns originals unchanged on failure or when no re-shoots are needed.
    """
    from features.fun_videos.pipeline import _finalize_prompt
    n = len(clip_paths)
    rng = pct_end - pct_start

    job.update(progress=pct_start, message=f"Director reviewing pass {pass_num} footage...")
    frames_per_clip = _extract_review_frames(clip_paths, clip_durations, job_dir, pass_num)

    job.update(progress=pct_start + max(1, rng // 4), message=f"Director analyzing pass {pass_num} clips...")
    user_idea = settings.get("video_prompt", "") or settings.get("user_direction", "")
    analysis = _director_analyze(llm_router, frames_per_clip, story_arc, user_idea, pass_num)

    regen_list = sorted(analysis.get("regenerate", []), key=lambda r: r["clip_idx"])
    if not regen_list:
        _log(f"[info] Director pass {pass_num}: {analysis.get('notes', 'all clips acceptable')} -- no re-shoots")
        return clip_paths, clip_durations, story_arc, assembled_path

    first_idx = regen_list[0]["clip_idx"]
    new_prompts = {r["clip_idx"]: r["new_prompt"] for r in regen_list}
    flagged = set(new_prompts)
    n_chain = n - first_idx
    _log(f"[info] Director pass {pass_num}: re-shooting clips {sorted(flagged)} -- {analysis.get('notes', '')}")

    new_clip_paths = list(clip_paths[:first_idx])
    new_clip_durs = list(clip_durations[:first_idx])
    new_arc = list(story_arc)

    # The clip at first_idx-1 was already trimmed + anchored in the main loop
    # (or by a previous director pass). Reuse the cached anchor PNG so the
    # re-shoot picks up exactly where the kept clip ends.
    prev_frame_path: str | None = (
        _find_anchor(job_dir, first_idx - 1, pass_num) if new_clip_paths else None
    )

    reshoot_pct_start = pct_start + max(1, rng // 3)

    for i in range(first_idx, n):
        if _stopped():
            return clip_paths, clip_durations, story_arc, assembled_path

        pct = reshoot_pct_start + int((pct_end - reshoot_pct_start) * (i - first_idx) / n_chain)
        arc_entry = story_arc[i] if isinstance(story_arc[i], dict) else {"prompt": str(story_arc[i]), "duration": 5.0}
        this_dur = arc_entry.get("duration", 5.0)

        if i in flagged:
            clip_prompt = new_prompts[i]
            new_entry = dict(arc_entry)
            new_entry["prompt"] = clip_prompt
            new_arc[i] = new_entry
            action = f"Director p{pass_num}: re-directing"
        else:
            clip_prompt = arc_entry.get("prompt", "")
            action = f"Director p{pass_num}: re-chaining"

        clip_out = str(job_dir / f"clip_{i:02d}_p{pass_num}_{job.id[:6]}.mp4")
        start_img = prev_frame_path if prev_frame_path else prepped_photo

        def _vprog(step, total, _p=pct, _ne=pct + max(1, (pct_end - reshoot_pct_start) // n_chain)):
            pp = _p + int(step / total * (_ne - _p)) if total > 0 else _p
            job.update(progress=pp, message=f"Director p{pass_num} clip {i + 1}: step {step}/{total}")

        job.update(progress=pct, message=f"{action} clip {i + 1}/{n}...")
        finalized = _finalize_prompt(clip_prompt, model_name)

        try:
            clip_path = video_generator.generate_video(
                image_path=start_img,
                prompt=finalized,
                out_path=clip_out,
                duration=this_dur,
                model_name=model_name,
                resolution=resolution,
                override_width=int(ow) if ow else None,
                override_height=int(oh) if oh else None,
                steps=steps,
                guidance=guidance,
                seed=seed,
                stop_check=_stopped,
                log_fn=_log,
                progress_fn=_vprog,
            )
        except Exception as e:
            log.warning("[director] Pass %d clip %d failed: %s -- keeping original", pass_num, i + 1, e)
            clip_path = clip_paths[i]

        actual = clip_path or clip_paths[i]
        actual_dur = probe_duration(actual) or this_dur

        new_clip_paths.append(actual)

        # Re-shot clips need the same chain-anchor treatment so junctions
        # remain frame-exact after re-assembly. Originals carried over
        # from the previous pass were already anchored.
        if i < n - 1:
            if clip_path and clip_path != clip_paths[i]:
                frame_out = str(job_dir / f"frame_p{pass_num}_{i:02d}.png")
                ok, anchored_dur = _chain_anchor(actual, frame_out)
                actual_dur = anchored_dur
                prev_frame_path = frame_out if ok else None
            else:
                prev_frame_path = _find_anchor(job_dir, i, pass_num + 1)

        new_clip_durs.append(actual_dur)

    if _stopped():
        return clip_paths, clip_durations, story_arc, assembled_path

    job.update(progress=pct_end - 1, message=f"Director pass {pass_num}: re-assembling...")
    new_assembled = str(job_dir / f"concat_p{pass_num}_{job.id[:6]}.mp4")
    if not _concat_clips(new_clip_paths, new_assembled):
        log.warning("[director] Pass %d re-assemble failed -- keeping previous result", pass_num)
        return clip_paths, clip_durations, story_arc, assembled_path

    return new_clip_paths, new_clip_durs, new_arc, new_assembled


_CHAIN_TRIM_RATIO = 0.88
_REANCHOR_EVERY_DEFAULT = 4


def _find_anchor(job_dir: Path, i: int, current_pass: int) -> str | None:
    """Find the most recent anchor PNG for clip index i.

    Anchors are written as frame_{i:02d}.png by the main loop and as
    frame_p{N}_{i:02d}.png by director pass N. Walks back from
    current_pass-1 to 0 to find the freshest cache.
    """
    for pn in range(max(0, current_pass - 1), 0, -1):
        p = job_dir / f"frame_p{pn}_{i:02d}.png"
        if p.exists():
            return str(p)
    p = job_dir / f"frame_{i:02d}.png"
    return str(p) if p.exists() else None


def _chain_anchor(clip_path: str, anchor_png: str, ratio: float = _CHAIN_TRIM_RATIO) -> tuple[bool, float]:
    """Trim clip to ratio*duration via re-encode and write last frame as PNG.

    The trim and the anchor frame share the same timestamp, so concatenating
    clips via hard cut produces a frame-exact junction (clip i ends on the
    same frame clip i+1 starts from). ratio=0.88 stays clear of LTX-2's
    final fade-out blur while keeping most of each clip.

    Returns (ok, new_duration). On failure, leaves clip_path untouched and
    returns (False, original_duration).
    """
    dur = probe_duration(clip_path) or 0.0
    if dur <= 1.0:
        return False, dur
    cut_to = dur * ratio

    # Re-encode trim so the new EOF lands exactly at cut_to (concat -c copy
    # would snap to the nearest keyframe, leaving the chain misaligned).
    trimmed = clip_path + ".trim.mp4"
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", clip_path,
         "-t", f"{cut_to:.4f}",
         "-c:v", "libx264", "-crf", "18", "-preset", "fast",
         "-pix_fmt", "yuv420p", "-an",
         trimmed],
        capture_output=True, timeout=120,
    )
    if r.returncode != 0 or not Path(trimmed).exists():
        log.warning("[chain] trim failed for %s: %s", clip_path, r.stderr.decode(errors='replace')[-400:])
        return False, dur
    os.replace(trimmed, clip_path)

    new_dur = probe_duration(clip_path) or cut_to
    # Pull the actual last frame of the trimmed clip (sseof reads from EOF).
    fr = subprocess.run(
        ["ffmpeg", "-y",
         "-sseof", "-0.10", "-i", clip_path,
         "-frames:v", "1", anchor_png],
        capture_output=True, timeout=30,
    )
    if fr.returncode != 0 or not Path(anchor_png).exists():
        log.warning("[chain] anchor extract failed for %s", clip_path)
        return False, new_dur
    return True, new_dur


def _bridge_steps(video_steps: int, model_name: str) -> int:
    """Return an appropriate step count for 2-second bridge clips.

    LTX-2 Distilled is trained for 4-8 steps; running 12+ steps overshoots its
    compressed denoising schedule and produces degraded output.
    Wan models need at least 12 steps to produce coherent short clips.
    """
    if "ltx" in model_name.lower():
        return min(video_steps, 8)
    return max(12, video_steps // 2)


# ── ffmpeg clip concatenation ─────────────────────────────────────────────────

def _concat_clips(clip_paths: list[str], out_path: str) -> bool:
    """Concatenate clips via ffmpeg concat demuxer, re-encoding to libx264.

    Re-encoding (rather than -c copy) sidesteps codec/profile mismatches
    between WanGP's raw output and the trimmed clips that came back through
    _chain_anchor's libx264 pass. CRF 18 fast preset keeps the quality hit
    minimal while guaranteeing the concat succeeds.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        for p in clip_paths:
            # ffmpeg concat demuxer: forward slashes required on Windows; single
            # quotes inside the file '...' entry must be doubled ("''"), not
            # backslash-escaped -- the concat demuxer uses its own quoting rules,
            # not shell quoting, so r"\'" is silently invalid and breaks on paths
            # with an apostrophe (e.g. "Andrew's PC").
            fwd = p.replace("\\", "/").replace("'", "''")
            f.write(f"file '{fwd}'\n")
        list_path = f.name
    try:
        r = subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", list_path,
                "-c:v", "libx264", "-crf", "18", "-preset", "fast",
                "-pix_fmt", "yuv420p",
                "-an",      # drop any audio from raw WanGP clips
                out_path,
            ],
            capture_output=True, timeout=600,
        )
        success = r.returncode == 0 and Path(out_path).exists()
        if not success:
            log.error("[multi] ffmpeg concat failed:\n%s", r.stderr.decode(errors="replace")[-2000:])
        return success
    except Exception as e:
        log.error("[multi] Clip concat exception: %s", e)
        return False
    finally:
        try:
            os.remove(list_path)
        except Exception:
            pass


# ── Prep phase (runs before GPU queue) ───────────────────────────────────────

def run_multi_prep(job, photo_path, settings):
    """Phase 0: Story arc + music direction — LLM only, no GPU.

    Runs outside the GPU queue concurrently with other queued jobs.
    Results are written into settings for run_multi_pipeline to consume.
    """
    from app import get_llm_router
    from features.fun_videos import analyzer
    llm_router = get_llm_router()

    n_clips          = int(settings.get("num_clips", 4))
    clip_dur         = float(settings.get("clip_duration", settings.get("video_duration", 5.0)))
    target_total     = settings.get("target_story_length")
    if target_total:
        target_total = float(target_total)
    user_idea        = settings.get("video_prompt", "") or settings.get("user_direction", "")
    skip_audio       = settings.get("skip_audio", False)
    instrumental     = settings.get("instrumental", False)
    lyric_direction  = settings.get("lyric_direction", "")
    model_name       = settings.get("model_name", "")

    # ── Reject T2V models ─────────────────────────────────────────────────
    # Multi-video chains clip i's last frame as clip i+1's start image. T2V
    # models do not accept image inputs -- WanGP silently drops them and
    # every clip is generated from prompt text only, so there is no visual
    # continuity at all. Fail loud rather than producing 6 disconnected clips.
    from features.fun_videos.video_generator import MODELS as _VG_MODELS
    _model_def = _VG_MODELS.get(model_name) if model_name else None
    if _model_def is not None and not _model_def.get("i2v", True):
        raise RuntimeError(
            f"Multi-video story requires an image-to-video model. {model_name} is "
            f"text-to-video and cannot chain clips. Pick an I2V model "
            f"(Wan2.1-I2V-* or LTX-2 *)."
        )

    # ── Story arc ─────────────────────────────────────────────────────────
    # Each generated clip is later trimmed to _CHAIN_TRIM_RATIO of its
    # generated length so junctions are frame-exact. Plan ~12% longer so
    # the final played output lands close to the user's requested target.
    plan_total = (target_total / _CHAIN_TRIM_RATIO) if target_total else None
    plan_clip_dur = clip_dur / _CHAIN_TRIM_RATIO

    job.update(progress=3, message="Planning story arc...")
    arc, arc_method = _generate_story_arc(
        llm_router, user_idea, n_clips, photo_path,
        progress_fn=lambda msg: job.update(message=msg),
        target_total_secs=plan_total,
        default_clip_dur=plan_clip_dur,
        model_name=model_name,
    )
    settings["_story_arc"] = arc
    arc_prompts = [a.get("prompt", "")[:40] if isinstance(a, dict) else str(a)[:40] for a in arc]
    if arc_method == "vision":
        log.info("[multi] Story arc via vision (%d clips): %s", n_clips, arc_prompts)
    elif arc_method == "text":
        log.info("[multi] Story arc via text-only (%d clips): %s", n_clips, arc_prompts)
        job.update(message="Story arc planned (photo analysis unavailable, used text)")
    else:
        log.warning("[multi] Story arc using built-in fallback: %s", arc_prompts)
        job.update(message="Story arc using default motion phases -- AI planning unavailable")

    if skip_audio:
        job.update(progress=10, message="Story arc ready, waiting for GPU...")
        return

    # ── Music direction ────────────────────────────────────────────────────
    music_prompt = settings.get("music_prompt", "")
    if not music_prompt:
        job.update(progress=7, message="Getting music direction…")
        try:
            provider = llm_router._provider()
            if provider == "ollama" and photo_path and os.path.isfile(photo_path):
                b64 = encode_image_b64(photo_path)
                frames = [b64] if b64 else []
            else:
                frames = []
            arc = settings.get("_story_arc", [])
            video_context = " ".join(
                a.get("prompt", "") if isinstance(a, dict) else str(a) for a in arc[:2]
            ) if arc else user_idea
            result = analyzer.generate_music_prompt(
                llm_router, frames, user_idea, video_prompt=video_context
            )
            music_prompt = result.get("music_prompt", "")
            if not settings.get("bpm") and result.get("bpm"):
                settings["bpm"] = result["bpm"]
            if music_prompt:
                settings["_prepped_music_prompt"] = music_prompt
                log.info("[multi] Music direction: %s", music_prompt[:80])
        except Exception as e:
            log.warning("[multi] Music prep failed: %s", e)

    # ── Lyrics ────────────────────────────────────────────────────────────
    if not instrumental and not settings.get("_prepped_lyrics") and music_prompt:
        job.update(progress=9, message="Writing lyrics…")
        try:
            lyrics = analyzer.generate_lyrics(
                llm_router, [], music_prompt, lyric_direction or user_idea,
            )
            if lyrics:
                settings["_prepped_lyrics"] = lyrics
        except Exception as e:
            log.warning("[multi] Lyrics prep failed: %s", e)

    job.update(progress=10, message="Story arc ready, waiting for GPU...")


# ── GPU phase ─────────────────────────────────────────────────────────────────

def run_multi_pipeline(job, photo_path, settings):
    """Multi-clip GPU pipeline: N chained clips → concat → audio → merge."""
    from app import get_llm_router, gallery_push
    from features.fun_videos import analyzer, video_generator, audio_generator
    from services.forge_client import unload_checkpoint, reload_checkpoint
    llm_router = get_llm_router()

    def _log(msg):
        log.info(msg)
        display = msg.removeprefix("[info] ").removeprefix("[error] ").removeprefix("[warning] ").removeprefix("[success] ")
        job.update(message=display)

    def _stopped():
        return job.stop_event.is_set()

    # ── Settings ──────────────────────────────────────────────────────────
    n_clips          = int(settings.get("num_clips", 4))
    clip_dur         = float(settings.get("clip_duration", settings.get("video_duration", 8.0)))
    model_name       = settings.get("model_name", "LTX-2 Dev19B Distilled")
    resolution       = settings.get("resolution", "580p")
    ow               = settings.get("override_width")
    oh               = settings.get("override_height")
    steps            = int(settings.get("video_steps", 30))
    guidance         = float(settings.get("video_guidance", 7.5))
    # LTX-2 Distilled denoising schedule is compressed into 4-8 steps -- cap to
    # prevent overshooting the schedule (bad output + unnecessary slowness).
    if "ltx" in model_name.lower() and "distilled" in model_name.lower():
        steps    = min(steps, 8)
        guidance = min(guidance, 4.0)
    seed             = int(settings.get("video_seed", -1))
    skip_audio       = settings.get("skip_audio", False)
    instrumental     = settings.get("instrumental", False)
    lyric_dir        = settings.get("lyric_direction", "")
    user_dir         = settings.get("user_direction", "")
    music_prompt     = settings.get("music_prompt", "") or settings.pop("_prepped_music_prompt", "")
    lyrics           = settings.pop("_prepped_lyrics", "")
    story_arc        = settings.pop("_story_arc", [])
    director_passes  = max(0, min(2, int(settings.get("director_passes", 0))))
    # Re-anchor was supposed to break compounding drift on LTX-2, but the cut
    # back to the source photo is itself visible as a hard jump in the final
    # video -- exactly what users complain about as "the clips don't connect."
    # Default OFF; rely on scene-anchored prompts to bound drift instead.
    # User can opt in via settings["reanchor_every"] for very long stories
    # where compounding outweighs the cut.
    reanchor_every   = int(settings.get("reanchor_every", 0))

    if not story_arc:
        base = (settings.get("video_prompt", "").strip() + ", ") if settings.get("video_prompt") else ""
        # Same trim compensation applied in run_multi_prep.
        fb_dur = clip_dur / _CHAIN_TRIM_RATIO
        story_arc = [
            {"prompt": base + _FALLBACK_PHASES[i % len(_FALLBACK_PHASES)], "duration": fb_dur}
            for i in range(n_clips)
        ]

    ts      = time.strftime("%Y-%m-%d")
    slug    = Path(photo_path).stem[:16].replace(" ", "_") if photo_path else "multivid"
    job_dir = OUTPUT_DIR / ts / f"multi_{slug}_{job.id}"
    job_dir.mkdir(parents=True, exist_ok=True)

    # Output dimensions
    if ow and oh:
        tw, th = int(ow), int(oh)
    else:
        _native = video_generator.MODELS.get(model_name, {}).get("res") or (1032, 580)
        tw, th = _native

    # Copy source photo for archival
    if photo_path and os.path.isfile(photo_path):
        src_copy = job_dir / f"source{Path(photo_path).suffix}"
        shutil.copy2(photo_path, src_copy)

    # ── Free VRAM before WanGP ────────────────────────────────────────────
    forge_unloaded = unload_checkpoint()
    if not skip_audio:
        try:
            from services.manager import stop_service, acestep_alive
            if acestep_alive():
                log.info("[multi] Stopping ACE-Step to free VRAM for WanGP")
                stop_service("acestep")
        except Exception as _e:
            log.debug("ACE-Step pre-stop skipped: %s", _e)

    # ── Phase 1: Generate clips sequentially ──────────────────────────────
    # Clips chain: clip_1 starts from the source photo; clips 2+ start from
    # the last frame of the previous clip extracted as PNG (lossless).
    # _chain_anchor trims each clip to _CHAIN_TRIM_RATIO of its generated
    # length and pulls the actual last frame of the trimmed clip, so
    # clip i ends on the same frame clip i+1 starts from. That makes
    # hard-cut concat seamless and removes the visible fade.
    # If anchoring fails for any clip, that clip resets to the source
    # photo rather than failing the entire job. Every reanchor_every
    # clips, we deliberately reset back to the source to break compounding
    # quality drift on long stories.

    # Compress the clip-generation progress window when director passes will follow
    # so there is room for review/re-shoot phases before the audio phase at 76%+.
    _clip_pct_range = {0: 65, 1: 45, 2: 35}.get(director_passes, 35)

    clip_paths: list[str] = []
    clip_durations: list[float] = []     # actual probed durations for director frame extraction
    prev_frame_path: str | None = None   # PNG chain frame from previous clip

    # Pre-process the original photo to exact WanGP dimensions
    prepped_photo: str | None = None
    if photo_path and os.path.isfile(photo_path):
        prepped_photo = _prep_photo(photo_path, tw, th, job_dir)

    _last_error: list[str | None] = [None]

    for i, clip_data in enumerate(story_arc):
        if _stopped():
            break

        # story_arc entries are dicts {"prompt": str, "duration": float}
        if isinstance(clip_data, dict):
            clip_prompt  = clip_data.get("prompt", "")
            this_clip_dur = max(4.0, min(15.0, float(clip_data.get("duration", clip_dur))))
        else:
            clip_prompt   = str(clip_data)
            this_clip_dur = clip_dur

        clip_num = i + 1
        pct_start = 10 + int((i / n_clips) * _clip_pct_range)
        pct_end   = 10 + int(((i + 1) / n_clips) * _clip_pct_range)

        job.update(progress=pct_start, message=f"Generating clip {clip_num} of {n_clips}...")

        def _video_progress(step, total, _s=pct_start, _e=pct_end, _cn=clip_num):
            pct = _s + int(step / total * (_e - _s)) if total > 0 else _s
            job.update(progress=pct, message=f"Clip {_cn}/{n_clips} -- step {step}/{total}")

        finalized = _finalize_prompt(clip_prompt, model_name)
        # Clip 1 anchors to source photo; clips 2+ chain from previous last frame.
        clip_start_image = prev_frame_path if prev_frame_path else prepped_photo
        clip_out = str(job_dir / f"clip_{i:02d}_{job.id[:6]}.mp4")

        effective_guidance = guidance

        clip_path = None
        for _attempt in range(2):
            if _stopped():
                break
            if _attempt > 0:
                # WanGP worker may have restarted (token mismatch, watchdog, etc.).
                # Give it a few seconds to settle before retrying the clip.
                _log(f"[info] Clip {clip_num}: retrying after worker restart...")
                time.sleep(4)
            try:
                clip_path = video_generator.generate_video(
                    image_path=clip_start_image,
                    prompt=finalized,
                    out_path=clip_out,
                    duration=this_clip_dur,
                    model_name=model_name,
                    resolution=resolution,
                    override_width=int(ow) if ow else None,
                    override_height=int(oh) if oh else None,
                    steps=steps,
                    guidance=effective_guidance,
                    seed=seed,
                    stop_check=_stopped,
                    log_fn=_log,
                    progress_fn=_video_progress,
                )
            except Exception as e:
                err = str(e)
                _log(f"[error] Clip {clip_num} failed (attempt {_attempt + 1}): {err}")
                _last_error[0] = err
                if "out of memory" in err.lower() or "cuda error" in err.lower():
                    from services import manager as _svc
                    threading.Thread(target=_svc.restart_service, args=("wangp",), daemon=True).start()
                    break  # OOM: don't retry, abort loop
                continue  # other exception: retry once
            if clip_path:
                break  # success

        if _stopped():
            break

        if not clip_path:
            _log(f"[error] Clip {clip_num} produced no output after retries -- stopping early")
            break

        clip_paths.append(clip_path)
        _log(f"[info] Clip {clip_num}/{n_clips} complete")

        # Trim each clip and extract the chain anchor at the SAME timestamp
        # so the next clip starts exactly where this one ends -- frame-exact
        # hard-cut concat with no visible jump.
        if i < len(story_arc) - 1:
            frame_out = str(job_dir / f"frame_{i:02d}.png")
            ok, new_dur = _chain_anchor(clip_path, frame_out)
            clip_durations.append(new_dur)
            if not ok:
                log.warning("[multi] Chain anchor failed for clip %d -- next clip resets to source", i + 1)
                prev_frame_path = None
            elif reanchor_every > 0 and (i + 1) % reanchor_every == 0:
                # Periodic re-anchor: clip (i+2) restarts from the source photo
                # to break the compounding quality drift on long stories.
                # Requires n_clips > reanchor_every to be worth doing.
                if n_clips > reanchor_every:
                    log.info("[multi] Re-anchoring clip %d back to source photo (every %d)",
                             i + 2, reanchor_every)
                    prev_frame_path = None
                else:
                    prev_frame_path = frame_out
            else:
                prev_frame_path = frame_out
        else:
            clip_durations.append(probe_duration(clip_path) or this_clip_dur)

    if _stopped():
        if forge_unloaded:
            reload_checkpoint()
        return

    if not clip_paths:
        if forge_unloaded:
            reload_checkpoint()
        raw = _last_error[0] or "No clips generated -- check WanGP is running"
        raise RuntimeError(f"Multi-video failed: {raw}")

    clips_end_pct = 10 + _clip_pct_range
    job.update(progress=clips_end_pct, message=f"All {len(clip_paths)} clips done -- generating transitions...")
    job.meta["clips_generated"] = len(clip_paths)
    job.meta["clip_paths"] = clip_paths

    # ── Phase 2a: Bridge clips ─────────────────────────────────────────────
    # Clips are now chained (each starts from the previous clip's last frame),
    # so adjacent clip boundaries share the same frame. A bridge from frame_X
    # to frame_X is a no-op that wastes a full GPU pass. Use crossfade instead.
    bridge_clips: list[str | None] = []

    # ── Phase 2b: Compile clips + bridges into one video ─────────────────
    compile_pct = clips_end_pct + 1
    job.update(progress=compile_pct, message="Compiling clips with transitions...")
    concat_path = str(job_dir / f"concat_{job.id[:6]}.mp4")

    if bridge_clips and not _stopped():
        from features.video_bridges.bridge_generator import compile_with_bridges
        compiled = compile_with_bridges(
            segment_paths=clip_paths,
            bridge_paths=bridge_clips,
            out_path=concat_path,
            resolution=resolution,
            log_fn=_log,
        )
        if not compiled:
            log.warning("[multi] compile_with_bridges failed -- falling back to xfade concat")
            compiled = None
    else:
        compiled = None

    if not compiled:
        # Hard-cut concat: chain_anchor aligned each clip's end with the
        # next clip's start image, so cuts are frame-exact and a crossfade
        # would only introduce visible cross-dissolve sludge.
        if not _concat_clips(clip_paths, concat_path):
            log.warning("[multi] Concat failed -- using first clip only")
            concat_path = clip_paths[0]

    # ── Director passes (optional) ────────────────────────────────────────
    # Each pass analyzes the assembled video, re-generates weak clips, and
    # re-assembles. Audio is only generated on the final cut.
    if director_passes >= 1 and not _stopped():
        _pass_ranges = {
            1: [(compile_pct + 2, 74)],
            2: [(compile_pct + 2, 60), (61, 74)],
        }.get(director_passes, [(compile_pct + 2, 74)])

        current_clips = clip_paths
        current_durs = clip_durations
        current_arc = story_arc
        current_assembled = concat_path

        for pass_idx, (p_start, p_end) in enumerate(_pass_ranges):
            if _stopped():
                break
            current_clips, current_durs, current_arc, current_assembled = _run_director_pass(
                job=job, llm_router=llm_router,
                clip_paths=current_clips, clip_durations=current_durs, story_arc=current_arc,
                prepped_photo=prepped_photo, assembled_path=current_assembled,
                settings=settings, job_dir=job_dir,
                pass_num=pass_idx + 1, pct_start=p_start, pct_end=p_end,
                _log=_log, _stopped=_stopped,
                video_generator=video_generator,
                model_name=model_name, resolution=resolution,
                ow=ow, oh=oh, steps=steps, guidance=guidance, seed=seed,
            )

        clip_paths = current_clips
        clip_durations = current_durs
        story_arc = current_arc
        concat_path = current_assembled
        job.meta["clip_paths"] = clip_paths

    job.meta["concat_path"] = concat_path

    # ── Phase 3: Audio ────────────────────────────────────────────────────
    if skip_audio:
        job.output = concat_path
        from core.inbox import copy_to_inbox; copy_to_inbox(job.output)
        job.message = f"Multi-video done ({len(clip_paths)} clips, no audio)"
        if forge_unloaded:
            reload_checkpoint()
        return

    # Warm up ACE-Step while we do analysis
    try:
        from services.manager import start_acestep, acestep_alive
        if not acestep_alive():
            threading.Thread(target=start_acestep, daemon=True).start()
            log.info("[multi] ACE-Step warm-up started after WanGP phase")
    except Exception as _e:
        log.debug("ACE-Step warm-up skipped: %s", _e)

    if not music_prompt:
        job.update(progress=78, message="Analyzing video for music direction…")
        try:
            frames = _sample_music_frames(concat_path, llm_router)
            if frames:
                result = analyzer.generate_music_prompt(llm_router, frames, user_dir)
                music_prompt = result.get("music_prompt", "indie folk, fingerpicked acoustic guitar, upright bass, brushed drums")
                if not settings.get("bpm") and result.get("bpm"):
                    settings["bpm"] = result["bpm"]
        except Exception as e:
            log.warning("[multi] Post-video music analysis failed: %s", e)
        if not music_prompt:
            music_prompt = "indie folk, fingerpicked acoustic guitar, upright bass, brushed drums"
    else:
        job.update(progress=78, message="Using pre-generated music direction…")

    if not instrumental and not lyrics:
        job.update(progress=80, message="Writing lyrics…")
        try:
            frames = _sample_music_frames(concat_path, llm_router)
            lyrics = analyzer.generate_lyrics(
                llm_router, frames, music_prompt, lyric_dir or user_dir,
            )
        except Exception as e:
            log.warning("[multi] Lyrics generation failed: %s", e)

    if not instrumental and not lyrics:
        lyrics = (
            "[verse]\nFrames in motion, stories unfold\n"
            "Every moment worth its weight in gold\n"
            "[chorus]\nLife in motion, frame by frame\n"
            "Nothing ever stays the same"
        )

    job.update(progress=82, message="Generating audio for full story...")
    forge_was_unloaded = forge_unloaded or unload_checkpoint()

    total_dur = probe_duration(concat_path)
    audio_dur = min(total_dur + 2.0, 300.0) if total_dur > 0 else float(sum(
        d.get("duration", clip_dur) if isinstance(d, dict) else clip_dur
        for d in story_arc
    ) + 2.0)

    def _audio_progress(elapsed_s):
        job.update(
            progress=82 + min(8, int(elapsed_s) // 10),
            message=f"Generating audio... {elapsed_s}s elapsed",
        )

    try:
        audio_path, audio_err = audio_generator.generate_audio(
            prompt=music_prompt,
            duration=audio_dur,
            output_dir=str(job_dir),
            audio_format=settings.get("audio_format", "mp3"),
            bpm=settings.get("bpm"),
            steps=int(settings.get("audio_steps", 8)),
            guidance=float(settings.get("audio_guidance", 7.0)),
            seed=-1,
            lyrics=lyrics,
            instrumental=instrumental,
            stop_event=job.stop_event,
            progress_cb=_audio_progress,
        )
    finally:
        if forge_was_unloaded:
            reload_checkpoint()

    if _stopped():
        return

    if not audio_path:
        _log(f"[warning] Audio failed: {audio_err} — returning video only")
        job.output = concat_path
        from core.inbox import copy_to_inbox; copy_to_inbox(job.output)
        job.message = f"Multi-video done ({len(clip_paths)} clips, audio failed)"
        return

    # ── Phase 4: Merge ────────────────────────────────────────────────────
    job.update(progress=92, message="Merging video + audio…")

    model_tag  = model_name.split()[0].lower()
    final_path = str(job_dir / f"multi_{model_tag}_{time.strftime('%H%M%S')}.mp4")

    merged = video_generator.merge_video_audio(concat_path, audio_path, final_path, log_fn=_log)

    if merged:
        # ── Phase 5: Upscale (optional) ───────────────────────────────────────
        upscale_on     = settings.get("upscale", True)
        upscale_scale  = float(settings.get("upscale_scale", 2.0))
        upscale_method = settings.get("upscale_method", "ffmpeg")
        if upscale_on and not _stopped():
            job.update(progress=95, message="Upscaling video...")
            try:
                from core.upscaler import upscale_video
                up_path = str(job_dir / f"multi_{model_tag}_{time.strftime('%H%M%S')}_up.mp4")
                up_out, up_err = upscale_video(merged, up_path,
                                               scale=upscale_scale, method=upscale_method)
                if up_out:
                    merged = up_out
                else:
                    log.warning("[multi] Upscale failed: %s -- using original", up_err)
            except Exception as _ue:
                log.warning("[multi] Upscale error: %s -- using original", _ue)

        job.output = merged
        from core.inbox import copy_to_inbox; copy_to_inbox(job.output)
        job.meta.update({"final_path": merged, "music_prompt": music_prompt})
        job.message = f"Multi-video complete! ({len(clip_paths)} clips)"

        # Gallery push
        try:
            norm = merged.replace("\\", "/")
            idx  = norm.lower().find("/output/")
            url  = norm[idx:] if idx != -1 else f"/output/{Path(merged).name}"
            elapsed = (time.time() - job.started_at) if job.started_at else None
            gallery_push(
                url, tab="create-videos",
                prompt=(story_arc[0].get("prompt", "") if isinstance(story_arc[0], dict) else str(story_arc[0]))[:120] if story_arc else "",
                model=model_name,
                metadata={
                    "path": merged,
                    "job_id": job.id,
                    "clips": len(clip_paths),
                    "story_arc": story_arc,
                    "elapsed_seconds": elapsed,
                    "model": model_name,
                    "duration_sec": total_dur or clip_dur * len(clip_paths),
                },
            )
        except Exception as e:
            log.warning("gallery_push failed: %s", e)

        # Session tracking
        try:
            from core.session import get_current as get_session
            get_session().add_file(Path(merged).name, "video", "fun_videos", path=merged)
        except Exception as e:
            log.warning("session.add_file failed: %s", e)

        # Clean up raw clip intermediates — only the merged file is needed
        for cp in clip_paths:
            if cp != merged:
                try:
                    os.remove(cp)
                except Exception:
                    pass
        if concat_path not in (merged, *clip_paths):
            try:
                os.remove(concat_path)
            except Exception:
                pass
    else:
        job.output = concat_path
        from core.inbox import copy_to_inbox; copy_to_inbox(job.output)
        job.message = f"Multi-video done ({len(clip_paths)} clips, audio merge failed)"
        try:
            from core.session import get_current as get_session
            get_session().add_file(Path(concat_path).name, "video", "fun_videos", path=concat_path)
        except Exception:
            pass
