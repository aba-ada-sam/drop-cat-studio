"""Multi-clip story pipeline: photo -> N chained video clips -> concat -> audio -> merge.

Each clip uses the last frame of the previous clip as its start image, enforcing
visual continuity across the full sequence.  Audio is a single ACE-Step pass over
the concatenated video -- never per-clip -- so the music has a natural arc.
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
from core.llm_client import TIER_FAST, TIER_BALANCED, encode_image_b64, parse_json_response
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

_FALLBACK_PHASES_CALM = [
    "ambient light shifts softly across the scene, subject completely still",
    "steam rises gently, background details settle, subject motionless",
    "shadow creeps slowly across the surface, atmosphere breathes",
    "curtain stirs slightly from unseen air, scene holds steady",
    "distant element drifts gently, foreground subject unchanged",
    "light quality shifts subtly, texture catches new angle",
    "water surface ripples faintly in the background",
    "cloud shadow passes overhead, scene breathes in stillness",
]

_FALLBACK_PHASES_NARRATIVE = [
    "slowly reaches forward with one hand, fingers extending toward something just out of frame",
    "turns their head toward the light, gaze shifting with deliberate focus",
    "takes one careful step forward, weight transferring with quiet intention",
    "lifts an object from the surface before them, examining it with both hands",
    "pauses and looks back over one shoulder, expression reading a moment of recognition",
    "leans in closer, attention narrowing on a detail in the scene",
    "stands up slowly, weight shifting as they rise to full height",
    "opens their hand and looks down at what rests in their palm",
]


# -- Audio-sync arc refinement ------------------------------------------------

_AUDIO_SYNC_SYSTEM = """\
You are synchronizing a music video. You have the original video clip prompts and,
for each clip, the lyrics being sung and an energy level (0.0=quiet verse, 1.0=full chorus).

Your task: add 4-8 words of visual context to each existing prompt so it reflects
the musical moment. Do NOT rewrite or shorten the prompt -- only APPEND to it.

Rules:
- High energy (>= 0.7) or energy-peak clips: append movement-amplifying words
  ("cresting into full intensity", "radiant burst", "peak moment of force")
- Low energy (< 0.3) clips: append atmosphere words
  ("quiet held moment", "suspended stillness", "hushed and waiting")
- If lyrics mention a concrete visual (fire, water, light, shadow, tears, gold, etc.):
  translate that into a visual word appended to the prompt
- NEVER paste the lyrics literally into a video prompt -- video models do not
  understand lyrics; translate them into physical scene elements
- Keep each appended addition SHORT (4-8 words, comma-separated from existing text)
- Return ONLY valid JSON: {"clips": [{"prompt": "full enhanced prompt"}, ...]}
  with exactly the same number of clips as the input.\
"""


def _refine_arc_with_audio(
    llm_router,
    story_arc: list[dict],
    clip_audio_ctx: list[dict],
    model_name: str,
    motion_style: str | None,
) -> list[dict]:
    """Refine story arc prompts with lyric and energy context from generated audio.

    One TIER_FAST LLM call for all clips -- appends visual hooks derived from
    what is being sung and how loud that moment is. Falls back to the original
    arc if the call fails or returns unparseable output.
    """
    has_lyrics = any(ctx.get("lyric_text", "").strip() for ctx in clip_audio_ctx)
    if not has_lyrics:
        log.info("[audio_sync] No lyric segments found -- skipping prompt refinement")
        return story_arc

    lines = []
    for i, (clip, ctx) in enumerate(zip(story_arc, clip_audio_ctx)):
        prompt = clip.get("prompt", "") if isinstance(clip, dict) else str(clip)
        lyrics = ctx.get("lyric_text", "")
        energy = ctx.get("energy_level", 0.5)
        peak_tag = " [ENERGY PEAK]" if ctx.get("has_energy_peak") else ""
        lines.append(
            f"Clip {i + 1}: {ctx['time_start']:.0f}s-{ctx['time_end']:.0f}s"
            f" | energy={energy:.1f}{peak_tag}"
            f" | lyrics: \"{lyrics}\"\n"
            f"  Prompt: {prompt}"
        )
    user_msg = (
        "\n\n".join(lines)
        + f"\n\nReturn enhanced prompts for all {len(story_arc)} clips."
    )

    try:
        text = llm_router.route(
            [{"role": "user", "content": user_msg}],
            tier=TIER_FAST,
            system=_AUDIO_SYNC_SYSTEM,
            max_tokens=900,
        )
        data = parse_json_response(text)
        if not data or not isinstance(data.get("clips"), list):
            log.warning("[audio_sync] Unparseable refinement response -- keeping original arc")
            return story_arc

        refined = []
        clips_data = data["clips"]
        for i, orig in enumerate(story_arc):
            if i < len(clips_data) and isinstance(clips_data[i], dict):
                new_prompt = (clips_data[i].get("prompt") or "").strip()
                if len(new_prompt) >= len(
                    orig.get("prompt", "") if isinstance(orig, dict) else str(orig)
                ):
                    entry = dict(orig) if isinstance(orig, dict) else {"prompt": str(orig), "duration": 5.0}
                    entry["prompt"] = _strip_camera_moves(new_prompt)
                    refined.append(entry)
                    continue
            refined.append(orig)

        log.info("[audio_sync] Refined %d/%d clip prompts with audio context",
                 sum(1 for a, b in zip(refined, story_arc)
                     if (a.get("prompt") if isinstance(a, dict) else str(a)) !=
                        (b.get("prompt") if isinstance(b, dict) else str(b))),
                 len(refined))
        return refined
    except Exception as e:
        log.warning("[audio_sync] Arc refinement failed (%s) -- keeping original arc", e)
        return story_arc


# -- Cloud-safe vision helpers ------------------------------------------------

_CLOUD_SCENE_DESCRIBE = """\
Examine this image and describe what you observe in objective, compositional terms.
Return 2-3 sentences covering:
- Subject type and physical characteristics (pose, build, clothing or lack thereof, expression)
- Setting: background, environment, location indicators
- Lighting: quality, direction, colour temperature
- Visual style: photographic technique, artistic treatment, mood

Be factual and clinical -- note what is present without moral judgement.
This description will be used to plan camera movements for a video production.
Do not generate creative content -- only describe what is visually present.\
"""


def _cloud_scene_describe(llm_router, frames_b64: list[str]) -> str:
    """Get a neutral scene description from a cloud vision API.

    Uses objective/compositional framing that bypasses Anthropic and OpenAI NSFW
    filters. Cloud APIs refuse to *generate* explicit content but will *describe*
    artistic nudity or suggestive images when asked for factual visual analysis.

    Returns a scene description string, or an empty string if no cloud key is
    configured. Returns a minimal placeholder if the cloud still refuses (very
    explicit content), so the pipeline can continue with text-only arc generation.
    """
    from core.keys import get_key
    cloud = "anthropic" if get_key("anthropic") else ("openai" if get_key("openai") else None)
    if not cloud:
        return ""
    try:
        text = llm_router.route_vision(
            "Describe the visual content of this image for a video production brief.",
            frames_b64,
            tier=TIER_FAST,
            system=_CLOUD_SCENE_DESCRIBE,
            max_tokens=200,
            force_provider=cloud,
        )
        from core.llm_router import _looks_like_safety_refusal
        if _looks_like_safety_refusal(text or ""):
            log.info("[multi] Cloud scene describe refused -- using minimal placeholder")
            return "a figure in an artistic pose, soft studio lighting, neutral background"
        return (text or "").strip()
    except Exception as e:
        log.warning("[multi] Cloud scene describe failed: %s", e)
        return ""


def _pick_cloud_provider(llm_router) -> str | None:
    """Return 'anthropic' or 'openai' if a cloud key exists, else None."""
    from core.keys import get_key
    if get_key("anthropic"):
        return "anthropic"
    if get_key("openai"):
        return "openai"
    return None


# -- Story arc generation ------------------------------------------------------

# Three system prompts:
#   CALM    -- environment-only motion, zero subject movement, stabilizer phrase appended.
#              Default for LTX (compressed schedule + low step count = ghost-risk on faces).
#   GENTLE  -- micro-motion on props/fabric/hands, subject face stays still. LTX dynamic mode.
#   KINETIC -- aggressive action verbs, Wan I2V only (25 steps handles it).

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
- NO camera moves of any kind -- no zoom, no pan, no push, no pull, no dolly, no tilt
- BANNED camera words (never use these): "zoom in", "zoom out", "zooms in", "zooms out",
  "pull back", "push in", "pan left", "pan right", "dolly", "tilt up", "tilt down",
  "camera moves", "camera pushes", "camera pulls", "camera zooms", "camera pans",
  "slow zoom", "gentle zoom", "subtle zoom", "zoom reveals", "zoom into"
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

ENERGY: micro-motion only. LTX-2 Distilled has a compressed denoising
schedule -- prompts must be SHORT (35-50 words) and describe only ONE small
change per clip. Long prompts, vague prompts, or prompts with multiple actions
cause scale drift and subject replacement by clip 3.

PROMPT SHAPE (mandatory for every clip):
  1. SUBJECT (10-15 words): Name the subject using their EXACT visual markers
     from the photo -- clothing colour, species, material, hair colour, etc.
     This anchors the model to the source image. Never omit this.
  2. ONE MICRO-MOTION (15-20 words): ONE small physical change happening in
     the scene. See motion hierarchy below.
  3. SCENE ANCHOR (8-12 words): Setting + visual style, restated every clip.
  4. CAMERA NOTE (4-5 words, LAST): "Static shot, fixed camera."

MOTION HIERARCHY -- choose the SAFEST option available for the scene:
  BEST (least risk of face blur):
    - Props/objects on a surface shift, slide, or settle
    - Background element stirs: leaf trembles, curtain moves, light shifts,
      shadow changes, screen flickers, steam rises, water ripples
    - Clothing/fabric: lapel stirs, sleeve shifts, collar moves, fabric settles
  GOOD (moderate risk):
    - Hand/arm: finger curls, hand shifts by a centimetre, wrist rotates slightly,
      pen taps once, paper lifts at edge
    - Shoulder/torso: chest rises with one breath, shoulders settle, weight shifts
  RISKY -- avoid unless the subject has no face (landscape, object, text):
    - Any head motion: head tilts, nods, turns
    - Any facial feature: eyes move, mouth opens, brows shift
    - Any species-specific face part: trunk sways, beak opens, ears flick,
      whiskers twitch, muzzle moves
  WHY: the face/head region has the highest spatial frequency. At low step
  counts the denoiser cannot maintain facial detail while also animating it --
  the result is temporal ghosting and blur. This applies to ALL subject types:
  people, animals, creatures, robots, characters. Keep the face still.

BANNED (cause LTX to replace the scene): walks, steps, runs, jumps, kneels,
rises, turns, exits, enters, climbs, pivots, reaches across.
BANNED: erupts, slams, explodes, thrashes, surges, screams, blasts, roars.
BANNED: "camera locked", "locked off", "locked frame", "no zoom" -- use "static shot, fixed camera" LAST only.

ARC: each clip = same scene, one different micro-detail. Vary which prop,
which fabric element, which background feature moves. No crescendo.\
"""

_STORY_ARC_LTX_ACTION = _STORY_ARC_BASE + """

ENERGY: deliberate physical motion. LTX-2 Dev13B runs 40 denoising steps and
can hold a complex subject through real movement -- strides, gestures, turns.
Prompts should be 45-65 words. Every clip must name the subject's exact visual
markers so the model re-anchors on each frame.

PROMPT SHAPE (mandatory every clip):
  1. SUBJECT (12-18 words): exact visual markers from the photo -- colour,
     material, size, style. For fantasy/painted subjects name the artistic style
     ("moss-covered giant", "painted illustration style"). Never omit this.
  2. ONE MAIN MOTION (20-30 words): a single deliberate physical action.
     Prefer clear, filmable actions: a stride, a lean, arms swinging, a turn of
     the head. Avoid vague energy words -- describe what the body DOES.
  3. SCENE ANCHOR (10-14 words): setting + lighting + visual style, restated
     every clip so the background does not drift.
  4. CAMERA NOTE (4-5 words, LAST): "wide shot, fixed camera" or similar.

ALLOWED MOTION: strides, steps, leans, turns, reaches, gestures, swings arms,
  tilts head, shifts weight, crouches, stands up, looks around. Moderate pace.
BANNED: erupts, slams, explodes, thrashes, roars, blasts -- these cause too
  much displacement and lose identity even at 40 steps.
BANNED: inventing subjects or props not in the photo.

CLIP COUNT vs PACING: 2-3 clips = single action arc. 4-5 clips = build and
  hold. Do not escalate to a climax -- keep energy steady and legible.\
"""

_STORY_ARC_NARRATIVE = _STORY_ARC_BASE + """

ENERGY: narrative motion. Each clip shows ONE purposeful action that advances
the story -- not kinetic chaos, not ambient stillness. Think: the moment a
character reaches for something, turns toward a sound, reacts to a discovery,
or crosses a threshold. Actions the audience reads as story beats.

NARRATIVE PRINCIPLES:
- Every action implies causality: something causes the subject to move, or the
  movement causes something. Do not describe motion for its own sake.
- Prefer legible, unambiguous gestures readable in one glance: reaches for,
  turns toward, kneels down, lifts, opens, steps to, looks up at.
- Arc: the clip sequence should feel like a mini-scene with a beginning,
  middle, and resolution -- not a random collection of motions.

PROMPT SHAPE (mandatory every clip):
  1. SUBJECT (12-16 words): exact visual markers from the photo -- clothing,
     species, material, expression. Name the subject precisely.
  2. ONE NARRATIVE ACTION (18-24 words): a single purposeful physical action
     a viewer reads as story. Describe WHAT they do and where attention goes.
  3. SCENE ANCHOR (10-14 words): setting + lighting + visual style, restated
     every clip so the background does not drift.
  4. CAMERA NOTE (4-5 words, LAST): "static shot, fixed camera" or "slow push
     in" only if the action strongly warrants it.

ALLOWED: reaches for / picks up / sets down, turns toward / looks at / glances
  away, takes one step toward / approaches, opens / closes / lifts / lowers,
  leans in / leans back / stands up / sits down, nods / shakes head (sparing).
BANNED: erupts, slams, explodes, thrashes, surges -- kills subject identity.
BANNED: ambient-only events (shadow shifting, steam, curtain) as PRIMARY action.
BANNED: abstract internal states ("contemplates", "realizes") -- show PHYSICAL
  actions a camera can capture.

ARC STRUCTURE:
  2 clips: action -> reaction
  3 clips: notice -> approach -> act
  4 clips: establish -> notice -> approach -> act
  5+ clips: establish -> notice -> approach -> act -> consequence

Do NOT escalate to chaos. The final clip is a natural endpoint, not a climax.\
"""

_STORY_ARC_CALM = _STORY_ARC_BASE + """

ENERGY: scene-hold mode. The subject does not move. Motion comes ONLY from the
environment already visible in the photo.

ALLOWED MOTION (environment only -- choose one per clip):
  - Light: a shadow shifts slightly across a surface, window light brightens or dims
  - Atmosphere: steam rises from a cup, smoke drifts from a fire, breath mist dissipates
  - Background: a distant leaf stirs, a curtain shifts from unseen air, water surface ripples
  - Gravity settle: fabric creases settle by weight, a prop shifts a millimetre by gravity
  - Weather: clouds pass overhead casting a shadow change, rain visible in background only

BANNED -- any motion involving the subject at all:
  - No head, face, eyes, mouth, brows, ears, trunk, snout, beak
  - No hands, arms, fingers, shoulders, torso breathing
  - No stepping, shifting weight, turning, reaching, swaying
  WHY: At 8-12 inference steps the denoiser cannot maintain facial/body detail while
  computing motion. Even a 1-degree head tilt causes temporal ghosting across the entire
  subject by frame 4. Environment-only motion keeps the subject pixel-stable.

PROMPT SHAPE (use this exact structure, every clip):
  1. SUBJECT (10-15 words): exact visual markers from photo -- clothing colour, species,
     material, hair colour. Describe the subject as completely still.
  2. ENVIRONMENT MOTION (12-18 words): ONE environmental change from the ALLOWED list.
     Must be physically present in or plausible from the original scene.
  3. SCENE ANCHOR (8-12 words): setting + visual style, restated every clip.
  4. CAMERA LOCK (fixed, always last): "Static shot, fixed camera."

ARC: same scene, one different environmental detail per clip.
Clips feel like a breathing photograph, not an action sequence.
No crescendo. No escalation. Pure scene preservation.\
"""


def _system_prompt_for_model(model_name: str, motion_style: str | None = None) -> str:
    is_ltx = "ltx" in (model_name or "").lower()
    is_ltx_dev13 = "dev13" in (model_name or "").lower()
    # Resolve default per model family when not explicitly chosen.
    # Dev13B defaults to dynamic (40 steps, handles real motion).
    # Other LTX models (Distilled, 8 steps) default to calm.
    resolved = motion_style or ("dynamic" if is_ltx_dev13 else ("calm" if is_ltx else "dynamic"))
    if resolved == "calm":
        return _STORY_ARC_CALM
    if resolved == "narrative":
        return _STORY_ARC_NARRATIVE
    if is_ltx:
        # Dev13B runs 40 steps -- can handle real motion.
        # Distilled runs 8 steps -- micro-motion only (GENTLE).
        if is_ltx_dev13:
            return _STORY_ARC_LTX_ACTION
        return _STORY_ARC_GENTLE
    return _STORY_ARC_KINETIC


def _generate_story_arc(
    llm_router,
    initial_idea: str,
    n_clips: int,
    photo_path: str | None,
    progress_fn=None,
    target_total_secs: float | None = None,
    default_clip_dur: float = 5.0,
    model_name: str = "",
    motion_style: str | None = None,
    continuation_mode: bool = False,
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
    is_ltx_dev13 = "dev13" in (model_name or "").lower()
    resolved_style = motion_style or ("dynamic" if is_ltx_dev13 else ("calm" if is_ltx else "dynamic"))

    # SCENE-HOLD EXTENSION (LTX-calm only):
    # Chained generation in LTX-calm mode is good at extending ONE shot, not
    # at telling a narrative across different shots. If we ask the LLM for N
    # different prompts (morning / afternoon / evening), the prompt weight in
    # calm-mode denoising overrides the chained start image, and the subject
    # visibly drifts each clip. Instead: use the user's idea verbatim as ALL
    # N prompts and let the chain-anchor (last-frame-of-clip-N becomes start-
    # frame-of-clip-N+1) carry continuity. Result: a single extended
    # atmospheric shot of the SAME subject in the SAME scene, breathing for
    # the requested total duration.
    if is_ltx and resolved_style == "calm" and not continuation_mode:
        if progress_fn:
            progress_fn("Locking scene for coherent extension...")

        user_text = (initial_idea or "").strip()

        # Vary the environmental effect per clip: one distinct atmospheric detail
        # per clip so there is a sense of time passing while the subject stays still.
        # Re-anchoring is OFF (reanchor_every=0 set by caller): each clip chains
        # from the previous clip's last frame, which already has the prior
        # atmosphere baked in. Re-anchoring to the static source resets that state
        # and triggers LTX background-fill heuristics (rain/debris artifacts).
        _CALM_EFFECTS = [
            "Warm light shifts slowly across the scene, casting long moving shadows.",
            "Steam or mist rises gently, wisps curling then dissipating in still air.",
            "A shadow creeps across the surface as light angle changes softly.",
            "Curtain or fabric edge stirs from unseen air, then settles.",
            "Cloud shadow passes overhead, light quality briefly dims then brightens.",
            "Distant background element drifts faintly, foreground subject unchanged.",
            "Light warms to golden hue, texture catches a new angle of shine.",
            "Faint atmospheric haze shifts in the background, depth breathes.",
        ]
        anchor = (
            "Breathing photograph, fixed camera. "
            "Subject holds perfectly still. "
            "{effect} "
            "Background unchanged, sky clear and steady, horizon locked."
        )
        fallback_base = (
            "breathing photograph, fixed wide shot, subject perfectly still, "
            "{effect} background unchanged, fixed frame, photorealistic"
        )
        clips = []
        for i in range(n_clips):
            effect = _CALM_EFFECTS[i % len(_CALM_EFFECTS)]
            if user_text:
                prompt = f"{user_text}. {anchor.format(effect=effect)}"
            else:
                prompt = fallback_base.format(effect=effect.lower())
            clips.append({"prompt": prompt, "duration": float(default_clip_dur)})

        log.info("[multi] Scene-hold extension (%s): %d clips, varied effects, first='%s...'",
                 resolved_style, n_clips, clips[0]["prompt"][:80])
        return clips, "scene-hold"

    if resolved_style == "calm":
        default_idea = "Create a calm breathing-photograph short film, environment-only motion, subject completely still"
    elif is_ltx and is_ltx_dev13:
        default_idea = "Create a short film with deliberate physical motion, subject moves with clear filmable actions, scene preserved"
    elif is_ltx:
        default_idea = "Create a calm observational short film, subtle continuous motion, scene preserved"
    else:
        default_idea = "Create an exciting action-packed short film"
    idea_text = (initial_idea or "").strip() or default_idea
    total_secs = target_total_secs or (n_clips * default_clip_dur)
    # style_hint is injected into the LLM prompt. Research findings per model:
    # LTX Distilled (8 steps): one motion per clip, cinematographic anchoring,
    #   explicit subject+background locks, no particle/atmospheric effects.
    # LTX Dev13B (40 steps): can handle deliberate motion; cinematic specificity
    #   (lens, lighting); "one clean move beats three messy ones".
    # Wan I2V: 80-120 words works well; structured sequence (opening -> camera
    #   move -> payoff); explicit camera verbs (pan, track, dolly, pull back);
    #   speed cues (slow, smooth glide); strong subject anchoring across clips.
    # Wan T2V: same as I2V but needs fuller scene description (no reference image).
    is_wan_t2v = "t2v" in (model_name or "").lower()
    if resolved_style == "calm":
        style_hint = "CALM -- environment-only motion, subject completely still, one environmental effect per clip, explicit background anchor"
    elif is_ltx and is_ltx_dev13:
        style_hint = (
            "LTX Dev13B (40 steps) -- deliberate physical motion OK (strides, gestures, turns). "
            "Use cinematic specificity: name the lens (85mm, 50mm), lighting quality (soft window light, golden hour). "
            "One clean motion per clip. Avoid stacking effects."
        )
    elif is_ltx:
        style_hint = (
            "LTX Distilled (8 steps) -- subtle motion only. "
            "Cinematographic framing required: 'static wide shot', 'fixed camera'. "
            "One motion element per clip. Anchor background explicitly ('background unchanged, sky steady')."
        )
    elif is_wan_t2v:
        style_hint = (
            "Wan T2V -- needs full scene description (no reference image). "
            "Vivid concrete nouns and active verbs. Specify time of day, lighting, atmosphere explicitly. "
            "Structure as sequence: opening -> action -> resolution. 80-120 words ideal."
        )
    else:
        style_hint = (
            "Wan I2V -- strong subject anchoring, handles kinetic action well. "
            "Structure as sequence: opening view -> camera movement -> reveal/payoff. "
            "Use cinematography verbs (pan, track, dolly, pull back, tilt). "
            "Add speed cues (slow glide, smooth track). 80-120 words ideal per clip."
        )
    # In calm mode, prefix the idea with a hard reminder so the LLM overrides any
    # kinetic words the user (or a JS default) may have included in the prompt.
    # The system prompt already bans "erupts, slams" etc., but a repeated prompt-level
    # note prevents the LLM from treating the user idea as higher-priority than the rules.
    if resolved_style == "calm":
        idea_display = f"[CALM MODE -- translate this idea into environment-only, subject-still motion]\n{idea_text}"
    else:
        idea_display = idea_text
    user_msg = (
        f"Target video model: {model_name or 'unknown'} ({style_hint})\n"
        f"Motion style: {resolved_style}\n"
        f"Initial idea: {idea_display}\n"
        f"Number of clips: {n_clips}\n"
        f"Target total story length: {int(total_secs)}s\n\n"
        f"Generate exactly {n_clips} sequential motion prompts as a story arc.\n\n"
        f"REQUIRED OUTPUT FORMAT -- respond with ONLY this JSON, no other text:\n"
        f'{{"clips": [{{"prompt": "prompt 1", "duration": 5}}, {{"prompt": "prompt 2", "duration": 8}}]}}'
    )
    system_prompt = _system_prompt_for_model(model_name, motion_style)

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
        if src < n_clips:
            log.warning("[multi] LLM returned %d clips but %d were requested -- "
                        "padding with cyclic repeats (clips %d+ will be duplicates)",
                        src, n_clips, src + 1)
        while len(result) < n_clips:
            result.append(dict(result[len(result) % src]))
        return result

    # Vision cascade: 4 attempts before falling back to built-in phases.
    #
    # Step 1a -- configured provider vision (Ollama or cloud).
    # Step 1b -- cloud vision with full story-arc prompt (explicit cloud force).
    # Step 1c -- cloud neutral-describe + cloud text-only arc (NSFW bypass):
    #            cloud refuses NSFW images when asked to *generate* creative
    #            content, but will *describe* the same image factually. We get
    #            a scene description, inject it into the text prompt, then ask
    #            a cloud text model to write the arc -- the text sanitizer in
    #            llm_router handles any explicit terms in the user's idea.
    # Step 2  -- text-only via configured provider (no image context).
    # Step 2b -- text-only via forced cloud (when Ollama is configured but busy).
    # Step 3  -- built-in fallback phases (always works).

    frames = []
    if photo_path and os.path.isfile(photo_path):
        b64 = encode_image_b64(photo_path)
        if b64:
            frames = [b64]

    if frames:
        if progress_fn:
            progress_fn("Planning story arc from photo...")

        # -- Step 1a: vision via configured provider --
        try:
            text = llm_router.route_vision(
                user_msg, frames,
                tier=TIER_BALANCED, system=system_prompt, max_tokens=1200,
                format_json=True,
            )
            result = _parse_clips(text)
            if result:
                return result, "vision"
            log.warning("[multi] Step 1a vision: unparseable response (%r) -- trying Step 1b", text[:200])
        except Exception as e:
            log.warning("[multi] Step 1a vision failed (%s) -- trying Step 1b", e)

        # -- Step 1b: explicit cloud vision (force Anthropic/OpenAI) --
        _cloud = _pick_cloud_provider(llm_router)
        if _cloud:
            try:
                text = llm_router.route_vision(
                    user_msg, frames,
                    tier=TIER_BALANCED, system=system_prompt, max_tokens=1200,
                    force_provider=_cloud,
                )
                result = _parse_clips(text)
                if result:
                    return result, "vision"
                log.warning("[multi] Step 1b cloud vision: unparseable response (%r) -- trying Step 1c", text[:200])
            except Exception as e:
                log.warning("[multi] Step 1b cloud vision failed (%s) -- trying Step 1c", e)

        # -- Step 1c: neutral cloud describe -> cloud text arc (NSFW bypass) --
        # Ask cloud for a factual scene description (passes NSFW filter),
        # then use that description as grounding for a text-only arc call.
        if progress_fn:
            progress_fn("Planning story arc (PG-13 vision path)...")
        scene_desc = _cloud_scene_describe(llm_router, frames)
        if scene_desc:
            _cloud_for_text = _pick_cloud_provider(llm_router)
            if _cloud_for_text:
                try:
                    enriched_msg = f"Scene in photo: {scene_desc}\n\n{user_msg}"
                    text = llm_router.route(
                        [{"role": "user", "content": enriched_msg}],
                        tier=TIER_BALANCED, system=system_prompt, max_tokens=1200,
                        force_provider=_cloud_for_text,
                    )
                    result = _parse_clips(text)
                    if result:
                        log.info("[multi] Step 1c cloud-describe arc succeeded (scene: %r)", scene_desc[:80])
                        return result, "cloud-describe"
                    log.warning("[multi] Step 1c cloud-describe: unparseable response -- trying text-only")
                except Exception as e:
                    log.warning("[multi] Step 1c cloud-describe arc failed (%s) -- trying text-only", e)

    # -- Step 2: text-only via configured provider (no image context) --
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
        log.warning("[multi] Step 2 text-only: unparseable response (%r) -- trying cloud text", text[:200])
    except Exception as e:
        log.warning("[multi] Step 2 text-only failed (%s) -- trying cloud text", e)

    # -- Step 2b: text-only forced cloud (when Ollama is configured but busy) --
    _cloud2 = _pick_cloud_provider(llm_router)
    if _cloud2:
        try:
            text = llm_router.route(
                [{"role": "user", "content": user_msg}],
                tier=TIER_BALANCED, system=system_prompt, max_tokens=1200,
                force_provider=_cloud2,
            )
            result = _parse_clips(text)
            if result:
                log.info("[multi] Step 2b cloud text arc succeeded (Ollama was busy)")
                return result, "cloud-text"
            log.warning("[multi] Step 2b cloud text: unparseable response -- using fallback")
        except Exception as e:
            log.warning("[multi] Step 2b cloud text failed (%s) -- using fallback", e)

    # -- Step 3: built-in fallback (always works, preserves user idea) --
    base = (initial_idea.strip() + ", ") if initial_idea else ""
    phases = _FALLBACK_PHASES_CALM if resolved_style == "calm" else _FALLBACK_PHASES
    return [
        {"prompt": base + phases[i % len(phases)], "duration": default_clip_dur}
        for i in range(n_clips)
    ], "fallback"


_DIRECTOR_SYSTEM_DYNAMIC = """\
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

_DIRECTOR_SYSTEM_CALM = """\
You are a film director reviewing footage from a calm, scene-hold short film.
You will be shown 2 frames per clip (early frame and late frame within each clip's segment).
Mode: CALM. The subject must be COMPLETELY STILL. Motion comes only from the environment.
Your job: identify clips that failed so they can be re-shot.

Rate each clip 1-5:
  5 = excellent -- subject pixel-stable, environment motion visible (light, steam, shadow, curtain), scene preserved
  4 = good -- minor flicker but acceptable
  3 = passable -- slight subject drift but scene held
  2 = weak -- subject visibly animated (head moved, eyes shifted, body moved), or scene replaced
  1 = bad -- subject replaced, scene changed to different location, or environment motion absent entirely

For clips rated <= 2: write a corrected calm prompt (35-55 words, environment-only motion).
  - Subject described as completely still
  - ONE environmental change only (light shift, steam, shadow, fabric settle, curtain stir)
  - Add camera lock phrase at end: "Static shot, fixed camera."
Apply TYPE rules:
  TYPE A (people/characters): name their visual markers (hair, clothing, skin) -- no body motion
  TYPE B (landscape/objects): describe background element change only

Be conservative: flag at most 2-3 clips. Only flag clips where the subject moved or scene changed.
A still-subject clip with barely visible environment motion is rated 3 (passable), NOT flagged.

Return ONLY valid JSON, no other text:
{"ratings": [5, 3, 2, 4], "regenerate": [{"clip_idx": 2, "new_prompt": "..."}], "notes": "one sentence what you changed and why"}
"""


def _director_system_for_style(motion_style: str | None) -> str:
    return _DIRECTOR_SYSTEM_CALM if motion_style == "calm" else _DIRECTOR_SYSTEM_DYNAMIC


def _extract_review_frames(
    clip_paths: list[str],
    clip_durations: list[float],
    job_dir: Path,
    pass_num: int,
    max_frames_per_clip: int = 12,
    downscale_long_edge: int = 384,
) -> list[list]:
    """Sample 1 frame/sec per clip (max 12), downscale via PIL, return b64 list/clip.

    Sampling rule:
      n_frames = min(max_frames_per_clip, max(1, round(duration)))
    so a 4s clip yields 4 frames, a 6s clip yields 6, a 12s clip yields 12,
    and any clip longer than max_frames_per_clip seconds gets the same 12
    frames spread evenly across the longer duration -- which is the
    "spread them out" requirement: more wall-time per frame, same coverage.

    Timestamps are centered in each "slot" (k + 0.5)/n so frames don't pile
    up at clip boundaries. Sampling from individual clip files avoids the
    xfade-overlap problem where assembled-video timestamps drift.

    Downscale path: ffmpeg extracts a full-res keyframe to a temp JPEG, then
    PIL opens it, thumbnails to downscale_long_edge on the long edge (LANCZOS,
    aspect preserved), and re-saves as JPEG q80. The two-stage extract+resize
    is cheap compared to the vision-LLM call that follows, and a 384px JPEG
    is roughly 1/6 the byte size of the native frame, which materially speeds
    up the AI response on cloud APIs (fewer tokens) and on Ollama (less KV).

    Returns list of [b64_1, b64_2, ...] per clip; entries may be empty on failure.
    """
    from PIL import Image as _PIL_Image

    result: list[list] = []
    for i, (path, dur) in enumerate(zip(clip_paths, clip_durations)):
        # Cap at max_frames_per_clip. For clips longer than that many seconds,
        # the same N frames spread evenly across the longer duration.
        n_frames = min(max_frames_per_clip, max(1, int(round(dur))))
        timestamps = [dur * (k + 0.5) / n_frames for k in range(n_frames)]

        frames: list[str] = []
        for k, t in enumerate(timestamps):
            raw_path   = job_dir / f"rv_p{pass_num}_c{i:02d}_f{k:02d}_raw.jpg"
            small_path = job_dir / f"rv_p{pass_num}_c{i:02d}_f{k:02d}.jpg"

            # ffmpeg seek+extract at native resolution (-q:v 2 = near-lossless).
            # Quality knob lives in the PIL re-save below, not here.
            r = subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", str(path),
                 "-vframes", "1", "-q:v", "2", str(raw_path)],
                capture_output=True, timeout=30,
            )
            if r.returncode != 0 or not raw_path.exists():
                continue

            try:
                # PIL downscale: long-edge -> downscale_long_edge, aspect preserved.
                # Image.thumbnail mutates in place and never upscales.
                img = _PIL_Image.open(raw_path).convert("RGB")
                img.thumbnail(
                    (downscale_long_edge, downscale_long_edge),
                    _PIL_Image.LANCZOS,
                )
                img.save(str(small_path), "JPEG", quality=80, optimize=True)
                b64 = encode_image_b64(str(small_path))
                if b64:
                    frames.append(b64)
            except Exception as e:
                log.warning("[director] PIL downscale failed for clip %d frame %d: %s",
                            i + 1, k, e)
            finally:
                # Always clean up both temp files, even on partial failure.
                try: raw_path.unlink(missing_ok=True)
                except Exception: pass
                try: small_path.unlink(missing_ok=True)
                except Exception: pass

        result.append(frames)
        if frames:
            log.info("[director] Clip %d/%d: extracted %d frames (dur %.1fs)",
                     i + 1, len(clip_paths), len(frames), dur)
    return result


def _director_analyze(
    llm_router, frames_per_clip: list[list], story_arc: list, user_idea: str,
    pass_num: int, motion_style: str | None = None, model_name: str = "",
) -> dict:
    """Send review frames to LLM vision; get per-clip ratings and re-shoot instructions.

    motion_style + model_name are surfaced into the user message so the LLM
    has explicit context for what the clips were supposed to look like:
      - LTX Distilled + calm   -> still subject, environment-only motion
      - LTX Distilled + gentle -> micro-motion, face stays still
      - Wan I2V + dynamic      -> kinetic action, identity holds through motion
    Without this hint the LLM applies whichever rubric the system prompt set,
    but doesn't know what *quality* to expect at the model's step budget.

    Returns {"ratings": [...], "regenerate": [{"clip_idx": int, "new_prompt": str}], "notes": str}.
    """
    n = len(frames_per_clip)
    clip_descs = []
    frame_counts = []
    for i, clip_data in enumerate(story_arc):
        p = clip_data.get("prompt", "") if isinstance(clip_data, dict) else str(clip_data)
        d = clip_data.get("duration", 5.0) if isinstance(clip_data, dict) else 5.0
        n_frames = len(frames_per_clip[i]) if i < len(frames_per_clip) else 0
        frame_counts.append(n_frames)
        clip_descs.append(f"Clip {i + 1} ({d:.0f}s, {n_frames} frames shown): {p}")

    # Model + motion context line. Empty when both are absent (legacy callers).
    ctx_bits = []
    if model_name:
        ctx_bits.append(f"Video model: {model_name}")
    if motion_style:
        ctx_bits.append(f"Motion style: {motion_style}")
    ctx_line = (" | ".join(ctx_bits) + "\n\n") if ctx_bits else ""

    user_msg = (
        f"Director pass {pass_num}. Original idea: {user_idea or 'no specific direction'}\n"
        f"{ctx_line}"
        f"Story arc ({n} clips):\n" + "\n".join(clip_descs) + "\n\n"
        f"Frames are sampled at ~1 per second per clip (max 12), spread evenly "
        f"across each clip's duration. Use them to judge whether the clip held "
        f"the source scene and whether motion matches the prompt's intent.\n\n"
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
            tier=TIER_BALANCED, system=_director_system_for_style(motion_style), max_tokens=800,
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
    motion_style: str | None = None,
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
    analysis = _director_analyze(
        llm_router, frames_per_clip, story_arc, user_idea, pass_num,
        motion_style=motion_style, model_name=model_name,
    )

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
        finalized = _finalize_prompt(clip_prompt, model_name, motion_style)

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
                negative_prompt=video_generator.negative_prompt_for(model_name, motion_style or "dynamic"),
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
    if new_clip_paths:
        _normalize_clip_for_concat(new_clip_paths[-1])
    if not _concat_with_xfade(new_clip_paths, new_clip_durs, new_assembled):
        log.warning("[director] Pass %d re-assemble failed -- keeping previous result", pass_num)
        return clip_paths, clip_durations, story_arc, assembled_path

    return new_clip_paths, new_clip_durs, new_arc, new_assembled


import re as _re_cam

_CAMERA_MOVE_RE = _re_cam.compile(
    r'\b(?:'
    r'zoom(?:s|ing|ed)?\s+(?:in|out|into|back)'
    r'|(?:slow|gentle|subtle|gradual|slight)\s+zoom'
    r'|zoom\s+(?:reveals?|shows?|uncovers?)'
    r'|pull(?:s|ing|ed)?\s+(?:back|out|away)'
    r'|push(?:es|ing|ed)?\s+in'
    r'|pan(?:s|ning|ned)?\s+(?:left|right|across|up|down|over)'
    r'|dolly(?:ing|ies|ied)?\s+(?:in|out|back)'
    r'|tilt(?:s|ing|ed)?\s+(?:up|down)'
    r'|track(?:s|ing|ed)?\s+(?:left|right|in|out|back)'
    r'|camera\s+(?:zooms?|pans?|pushes?|pulls?|moves?|drifts?|sweeps?|tracks?|dollies?|tilts?)'
    r')',
    _re_cam.IGNORECASE,
)


def _strip_camera_moves(prompt: str) -> str:
    """Remove zoom/pan/dolly/tilt camera instructions from a clip prompt.

    Applied to every LLM-generated clip prompt before it reaches WanGP.
    WanGP and LTX interpret camera words literally and apply the motion,
    producing inconsistent clip-to-clip camera work.
    """
    cleaned = _CAMERA_MOVE_RE.sub('', prompt)
    cleaned = _re_cam.sub(r'[ ,;]+', ' ', cleaned).strip().strip(',').strip(';').strip()
    if cleaned != prompt:
        log.info("[prompt] stripped camera moves: %r -> %r", prompt[:60], cleaned[:60])
    return cleaned


def _enforce_static_camera(prompt: str) -> str:
    """Strip camera moves then append the static-camera constraint.

    The model defaults to camera motion when subject motion is ambiguous.
    Stating the constraint positively at the END of the prompt overrides
    that default -- the model reads the last tokens with highest weight.
    """
    base = _strip_camera_moves(prompt).rstrip('. ,')
    return base + ", static shot, fixed camera" if base else "static shot, fixed camera"


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
    if p.exists():
        return str(p)
    # No anchor found. Caller will fall back to prepped_photo, which can
    # cause a visible subject discontinuity at clip i's junction. Warn so
    # the failed anchor write upstream is at least visible in logs.
    if current_pass > 0:
        log.warning("[anchor] No cached frame for clip %d at pass %d -- "
                    "falling back to source photo (junction may jump)",
                    i, current_pass)
    return None


def _chain_anchor(clip_path: str, anchor_png: str, ratio: float = _CHAIN_TRIM_RATIO) -> tuple[bool, float]:
    """Trim clip to ratio*duration via re-encode and write last frame as PNG.

    CRITICAL ORDER: extract PNG from the ORIGINAL WanGP output FIRST, then
    re-encode. CRF-15 H.264 introduces macroblock artifacts; if the PNG is
    extracted after re-encoding those artifacts become the conditioning frame
    for the next clip, and the model reinforces them on every subsequent clip
    -- cumulative skin "pox" marks and texture degradation. The clean diffusion
    output must be the source for the anchor frame.

    Returns (ok, new_duration). On failure, leaves clip_path untouched and
    returns (False, original_duration).
    """
    dur = probe_duration(clip_path) or 0.0
    if dur <= 1.0:
        return False, dur
    cut_to = dur * ratio
    seek_t = max(0.0, cut_to - 0.05)

    # Step 1: Extract anchor PNG from ORIGINAL output BEFORE any re-encode.
    anchor_ok = False
    for _cmd in [
        ["ffmpeg", "-y", "-ss", f"{seek_t:.4f}", "-i", clip_path,
         "-frames:v", "1", anchor_png],
        ["ffmpeg", "-y", "-i", clip_path,
         "-ss", f"{seek_t:.4f}", "-frames:v", "1", anchor_png],
    ]:
        fr = subprocess.run(_cmd, capture_output=True, timeout=30)
        if fr.returncode == 0 and Path(anchor_png).exists():
            anchor_ok = True
            break
    if not anchor_ok:
        log.warning("[chain] anchor extract failed for %s", clip_path)

    # Step 2: Re-encode-trim for concat AFTER the PNG is safely extracted.
    # concat -c copy would snap to the nearest keyframe, leaving the chain
    # misaligned; re-encode lands the EOF exactly at cut_to.
    trimmed = clip_path + ".trim.mp4"
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", clip_path,
         "-t", f"{cut_to:.4f}",
         "-c:v", "libx264", "-crf", "15", "-preset", "fast",
         "-pix_fmt", "yuv420p", "-an",
         trimmed],
        capture_output=True, timeout=120,
    )
    if r.returncode != 0 or not Path(trimmed).exists():
        log.warning("[chain] trim failed for %s: %s", clip_path, r.stderr.decode(errors="replace")[-400:])
        return anchor_ok, dur
    os.replace(trimmed, clip_path)

    new_dur = probe_duration(clip_path) or cut_to
    return anchor_ok, new_dur


_ANCHOR_BLEND_ALPHA = 0.95  # 95% last frame (motion continuity) + 5% source (identity)


def _blend_anchor_with_source(anchor_png: str, source_photo: str,
                               alpha: float = _ANCHOR_BLEND_ALPHA) -> None:
    """Blend chain anchor frame with original source photo to prevent character drift.

    Without blending, small appearance errors compound across clips: the model
    slightly changes hair color or skin tone in clip 2, that becomes the anchor
    for clip 3 which drifts further, and by clip 6 the character looks like a
    different person. Injecting 18% of the original source on every anchor acts
    as a gravity well that pulls appearance back toward the source on each transition
    without snapping visually or breaking motion continuity.

    alpha=0.82 is empirically safe: enough last-frame dominance to carry motion
    and environment state; enough source-photo injection to kill drift within 3-4
    clips even on LTX-2 with active scene prompts.
    """
    try:
        from PIL import Image
        anc = Image.open(anchor_png).convert("RGB")
        src = Image.open(source_photo).convert("RGB").resize(anc.size, Image.LANCZOS)
        Image.blend(src, anc, alpha=alpha).save(anchor_png)
    except Exception as exc:
        log.warning("[chain] anchor blend with source failed (using raw anchor): %s", exc)


# -- ffmpeg clip concatenation -------------------------------------------------

def _normalize_clip_for_concat(clip_path: str) -> bool:
    """Re-encode a clip to libx264 CRF 15 in-place.

    Called on the last clip only, which skips _chain_anchor (no next clip
    to anchor to). Without this, the last clip stays in WanGP's native
    format while all other clips are CRF-15 libx264 from _chain_anchor.
    The mismatch forces _concat_clips to re-encode everything a second time.
    After normalizing the last clip here, all clips are in the same format
    and _concat_clips can stream-copy with no quality loss.
    """
    tmp = clip_path + ".norm.mp4"
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", clip_path,
         "-c:v", "libx264", "-crf", "15", "-preset", "fast",
         "-pix_fmt", "yuv420p", "-an", tmp],
        capture_output=True, timeout=120,
    )
    if r.returncode == 0 and Path(tmp).exists():
        os.replace(tmp, clip_path)
        return True
    log.warning("[concat] last-clip normalize failed -- concat will re-encode: %s",
                r.stderr.decode(errors="replace")[-200:])
    try:
        Path(tmp).unlink(missing_ok=True)
    except OSError:
        pass
    return False


def _concat_clips(clip_paths: list[str], out_path: str) -> bool:
    """Concatenate clips via ffmpeg concat demuxer (stream copy).

    All clips must be in the same libx264/yuv420p format before calling
    this function. _chain_anchor normalizes clips 0..N-2 at CRF 15;
    callers must normalize clip N-1 via _normalize_clip_for_concat first.
    Stream copy avoids a second lossy encode of clips that already went
    through _chain_anchor's CRF-15 pass.

    Falls back to re-encoding at CRF 15 if stream copy fails (e.g. any
    clip was not normalized -- better one extra encode than a broken output).
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
                "-c", "copy",
                "-an",
                out_path,
            ],
            capture_output=True, timeout=600,
        )
        if r.returncode == 0 and Path(out_path).exists():
            return True
        # Stream copy failed -- fall back to re-encode so the job doesn't die
        log.warning("[multi] stream-copy concat failed, falling back to re-encode: %s",
                    r.stderr.decode(errors="replace")[-400:])
        try:
            Path(out_path).unlink(missing_ok=True)
        except OSError:
            pass
        r2 = subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", list_path,
                "-c:v", "libx264", "-crf", "15", "-preset", "fast",
                "-pix_fmt", "yuv420p",
                "-an",
                out_path,
            ],
            capture_output=True, timeout=600,
        )
        success = r2.returncode == 0 and Path(out_path).exists()
        if not success:
            log.error("[multi] ffmpeg concat fallback failed:\n%s",
                      r2.stderr.decode(errors="replace")[-2000:])
        return success
    except Exception as e:
        log.error("[multi] Clip concat exception: %s", e)
        return False
    finally:
        try:
            os.remove(list_path)
        except Exception:
            pass


_XFADE_DUR = 0.25  # seconds -- 6 frames @25fps, 4 frames @16fps; invisible as a wipe but smooths seams


def _concat_with_xfade(clip_paths: list[str], clip_durs: list[float], out_path: str,
                        fade_dur: float = _XFADE_DUR) -> bool:
    """Concatenate clips with a short alpha-blend crossfade at each junction.

    Hard-cut concat produces a visible seam even with frame-exact chaining.
    Diffusion models do not generate a first frame pixel-identical to their
    conditioning image -- there is always a slight luminance shift or color-cast
    at the start of each clip. A 0.25s crossfade makes every transition invisible
    without the dreaded "cross-dissolve sludge" of long fades.

    Uses ffmpeg xfade filter with transition=fade. Falls back to _concat_clips
    (hard cut) if xfade encode fails or if only 1 clip.
    """
    n = len(clip_paths)
    if n <= 1:
        return _concat_clips(clip_paths, out_path)

    # Cap fade_dur to at most 20% of the shortest clip to avoid xfade consuming tiny clips
    min_dur = min(clip_durs[:n]) if clip_durs else 3.0
    fade = min(fade_dur, min_dur * 0.20)
    if fade < 0.04:
        return _concat_clips(clip_paths, out_path)

    # Build ffmpeg inputs
    inputs = []
    for p in clip_paths:
        inputs += ["-i", p]

    # Build chained xfade filter_complex.
    # Offset for xfade i is measured in the OUTPUT timeline from t=0.
    # O_0 = d[0] - fade
    # O_i = O_{i-1} + d[i] - fade  (each xfade shortens the output by fade)
    filter_parts = []
    cumulative_offset = 0.0
    prev_label = "[0:v]"

    for i in range(1, n):
        offset = max(0.01, cumulative_offset + clip_durs[i - 1] - fade)
        out_label = "[outv]" if i == n - 1 else f"[xf{i}]"
        filter_parts.append(
            f"{prev_label}[{i}:v]xfade=transition=fade"
            f":duration={fade:.4f}:offset={offset:.4f}{out_label}"
        )
        prev_label = out_label
        cumulative_offset = offset

    filter_complex = ";".join(filter_parts)

    cmd = ["ffmpeg", "-y"] + inputs + [
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-c:v", "libx264", "-crf", "15", "-preset", "fast",
        "-pix_fmt", "yuv420p", "-an",
        out_path,
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=600)
    if r.returncode == 0 and Path(out_path).exists():
        return True

    log.warning("[multi] xfade concat failed -- falling back to hard cut: %s",
                r.stderr.decode(errors="replace")[-400:])
    try:
        Path(out_path).unlink(missing_ok=True)
    except OSError:
        pass
    return _concat_clips(clip_paths, out_path)


def _normalize_video_for_concat(src: str, dst: str, width: int, height: int, fps: int = 25, trim_to: float | None = None) -> bool:
    """Re-encode a video to exact dimensions + libx264 for stitching with AI clips.

    Pads with black bars when the source aspect ratio differs from the target
    (e.g. portrait phone video -> landscape AI clip). Audio is dropped so the
    stitched silent video goes through the normal audio generation path.
    fps must match the AI clips being concatenated (LTX=25, Wan=16).
    trim_to: if set, output only the first trim_to seconds (cuts faded tail before
    the AI continuation begins so there is no visible seam).
    """
    try:
        cmd = ["ffmpeg", "-y", "-i", src]
        if trim_to is not None:
            cmd += ["-t", f"{trim_to:.3f}"]
        cmd += [
            "-vf", (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
            ),
            "-r", str(fps), "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-an", dst,
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        ok = r.returncode == 0 and Path(dst).exists()
        if not ok:
            log.warning("[multi] normalize_video_for_concat failed:\n%s",
                        r.stderr.decode(errors="replace")[-1000:])
        return ok
    except Exception as e:
        log.warning("[multi] normalize_video_for_concat exception: %s", e)
        return False


# -- Prep phase (runs before GPU queue) ---------------------------------------

def _free_vram_for_llm(llm_router, reason: str) -> bool:
    """Evict WanGP from VRAM before an Ollama LLM/vision call.

    Only acts when:
      - The configured provider is Ollama (local, needs VRAM headroom)
      - WanGP is currently loaded (gpu.current == "wangp")
      - No GPU job is actively running (safe to evict without killing a live gen)

    Returns True if eviction happened (caller may proceed with vision),
    False if WanGP is busy (caller should skip vision and use text-only fallback).
    """
    if llm_router._provider() != "ollama":
        return True  # cloud providers don't load into local VRAM
    from core.gpu_orchestrator import gpu as _gpu
    if _gpu.current != "wangp":
        return True  # WanGP not loaded, no VRAM conflict
    # Check whether a GPU job is currently running
    try:
        from app import get_job_manager as _gjm
        from core.job_manager import GPU_JOB_TYPES
        _busy = any(
            j.type in GPU_JOB_TYPES and j.status == "running"
            for j in _gjm()._jobs.values()
        )
    except Exception:
        _busy = True  # assume busy if we can't check
    if _busy:
        log.info("[prep] WanGP is running a job -- skipping Ollama vision for %s (will use text-only)", reason)
        return False
    log.info("[prep] Evicting idle WanGP to free VRAM for Ollama (%s)", reason)
    _gpu.acquire("ollama", reason=f"prep-phase LLM: {reason}")
    return True


def _release_ollama_vram():
    """Send keep_alive=0 to Ollama after prep LLM calls so WanGP gets full VRAM.

    The GPU orchestrator does this automatically when gpu.acquire('wangp') is
    called, but that happens later in run_multi_pipeline. Doing it here too
    means the VRAM is freed as soon as prep finishes rather than holding the
    model for the duration of the GPU queue wait.
    """
    try:
        from core.gpu_orchestrator import gpu as _gpu
        if _gpu.current == "ollama":
            _gpu._ollama_unload()
            log.info("[prep] Ollama model unloaded after prep phase")
    except Exception as e:
        log.debug("[prep] Ollama unload skipped: %s", e)


def run_multi_prep(job, photo_path, settings):
    """Phase 0: Story arc + music direction -- LLM only, no GPU.

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
    motion_style     = settings.get("motion_style") or None  # None -> auto-resolve per model

    # Run audio event detection if audio_path is set (from audio-first or future beat sync).
    _raw_audio = settings.get("audio_path", "") or settings.get("_audio_path", "")
    if _raw_audio and os.path.isfile(_raw_audio) and "_audio_events" not in settings:
        try:
            from features.fun_videos.audio_analyzer import detect_audio_events
            settings["_audio_events"] = detect_audio_events(_raw_audio)
            log.info("[multi] Audio events detected for beat-snap")
        except Exception as _ae:
            log.warning("[multi] Audio event detection failed: %s", _ae)

    # -- Video continuation mode -------------------------------------------
    # If a start_video_path is provided and video_mode == 'continuation',
    # extract the last frame to use as the LLM vision anchor AND as the
    # start image for clip 1 (so the AI continues exactly where the video ended).
    # 'inspired' mode just extracts the first frame for visual context.
    start_video_path = settings.get("start_video_path") or ""
    video_mode       = settings.get("video_mode", "continuation")
    effective_photo  = photo_path

    if start_video_path and os.path.isfile(start_video_path):
        from core.ffmpeg_utils import extract_last_frame_to_file, extract_first_frame_to_file
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp_path = tmp.name
        tmp.close()
        if video_mode == "continuation":
            seek_s = settings.get("start_video_seek_seconds")
            if extract_last_frame_to_file(start_video_path, tmp_path, seek_seconds=seek_s):
                effective_photo = tmp_path
                settings["_start_video_last_frame"] = tmp_path
                settings["_prepend_original_video"] = start_video_path
                # Store the actual trim point so the stitch cuts the original
                # exactly where the AI clip begins -- no faded tail, no seam.
                from core.ffmpeg_utils import probe_duration as _pd
                _dur = _pd(start_video_path)
                _trim = seek_s if seek_s is not None else (_dur * 0.85 if _dur > 0 else 0)
                settings["_prepend_video_trim_seconds"] = _trim
                if seek_s is not None:
                    log.info("[multi] Continuation mode: frame at %.2fs extracted from %s", seek_s, start_video_path)
                else:
                    log.info("[multi] Continuation mode: last frame extracted from %s", start_video_path)
            else:
                log.warning("[multi] Could not extract last frame from %s", start_video_path)
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        else:  # inspired -- first frame used for LLM vision only, not stored beyond prep
            if extract_first_frame_to_file(start_video_path, tmp_path):
                effective_photo = effective_photo or tmp_path
                log.info("[multi] Inspired mode: first frame extracted from %s", start_video_path)
                settings["_inspired_frame_tmp"] = tmp_path  # track for cleanup after prep
            else:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # -- Reject T2V models -------------------------------------------------
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

    # -- Story arc ---------------------------------------------------------
    # Each generated clip is later trimmed to _CHAIN_TRIM_RATIO of its
    # generated length so junctions are frame-exact. Plan ~12% longer so
    # the final played output lands close to the user's requested target.
    plan_total = (target_total / _CHAIN_TRIM_RATIO) if target_total else None
    plan_clip_dur = clip_dur / _CHAIN_TRIM_RATIO

    # VRAM guard: if Ollama is the LLM provider and WanGP is idle-but-loaded,
    # evict WanGP now so Ollama's vision model fits. If WanGP is actively
    # running a job, _free_vram_for_llm returns False and we skip vision so
    # we don't interrupt a live generation. gpu.acquire("wangp") in
    # run_multi_pipeline will reclaim VRAM from Ollama at the right time.
    _vision_ok = _free_vram_for_llm(llm_router, "story arc")
    # If VRAM is not free for Ollama, generate arc without passing photo frames
    # (_generate_story_arc has a text-only fallback path when photo is None).
    arc_photo = effective_photo if _vision_ok else None

    job.update(progress=3, message="Planning story arc...")
    is_continuation = bool(start_video_path) and video_mode == "continuation"
    # In continuation mode, force "gentle" so the AI clip actually moves --
    # "calm" (static subject) looks wrong when stitched after a live video.
    arc_motion = "gentle" if is_continuation and (motion_style or "calm") == "calm" else motion_style
    arc, arc_method = _generate_story_arc(
        llm_router, user_idea, n_clips, arc_photo,
        progress_fn=lambda msg: job.update(message=msg),
        target_total_secs=plan_total,
        default_clip_dur=plan_clip_dur,
        model_name=model_name,
        motion_style=arc_motion,
        continuation_mode=is_continuation,
    )
    settings["_story_arc"] = arc

    # Snap clip boundaries to strong beats if audio analysis is available.
    _a_events = settings.get("_audio_events", {})
    if _a_events and settings.get("_story_arc"):
        try:
            from features.fun_videos.audio_analyzer import snap_durations_to_beats
            from core.ffmpeg_utils import probe_duration as _pd
            _a_path = settings.get("audio_path", "")
            _a_dur = float(_a_events.get("duration") or (_pd(_a_path) if _a_path else 0) or 0)
            if _a_dur > 0:
                settings["_story_arc"] = snap_durations_to_beats(
                    settings["_story_arc"], _a_events, _a_dur, snap_window=2.0
                )
                log.info("[multi] Story arc durations snapped to beats")
        except Exception as _e:
            log.warning("[multi] Beat-snap failed (non-fatal): %s", _e)

    # Extract a concrete subject description from the source image so every
    # clip prompt is anchored to the actual visual content, not the LLM's
    # memory of what it hallucinated for clip 1.
    subject_anchor = ""
    if arc_photo and os.path.isfile(arc_photo):
        try:
            b64 = encode_image_b64(arc_photo)
            if b64:
                anchor_raw = llm_router.route_vision(
                    "Describe the main subject in this image in 20-25 words. "
                    "Include: exact hair color and style, clothing color and type, "
                    "skin tone, eye color, distinguishing features, species if not human, "
                    "any accessories. Be specific and visual only -- no emotions or context. "
                    "Example: 'Red-haired woman, loose shoulder curls, blue denim jacket, "
                    "white t-shirt, pale freckled skin, hazel eyes, small silver earrings.'",
                    [b64],
                    tier=TIER_FAST,
                    max_tokens=90,
                )
                subject_anchor = anchor_raw.strip().strip('"').strip("'")
                if subject_anchor and not subject_anchor.endswith("."):
                    subject_anchor += "."
                log.info("[multi] Subject anchor: %s", subject_anchor)
        except Exception as e:
            log.warning("[multi] Subject anchor extraction failed (non-fatal): %s", e)
    settings["_subject_anchor"] = subject_anchor

    # Inspired mode: first-frame temp file was only needed for story-arc vision.
    # Delete it now so it doesn't leak into the OS temp dir.
    _inspired_tmp = settings.pop("_inspired_frame_tmp", None)
    if _inspired_tmp:
        try:
            os.unlink(_inspired_tmp)
        except OSError:
            pass

    arc_prompts = [a.get("prompt", "")[:40] if isinstance(a, dict) else str(a)[:40] for a in arc]
    if arc_method == "vision":
        log.info("[multi] Story arc via vision (%d clips): %s", n_clips, arc_prompts)
    elif arc_method == "cloud-describe":
        log.info("[multi] Story arc via cloud-describe PG-13 path (%d clips): %s", n_clips, arc_prompts)
        job.update(message="Story arc planned (image described via cloud, prompts generated by AI)")
    elif arc_method in ("text", "cloud-text"):
        log.info("[multi] Story arc via %s (%d clips): %s", arc_method, n_clips, arc_prompts)
        job.update(message="Story arc planned (text-only, no photo analysis)")
    elif arc_method == "scene-hold":
        log.info("[multi] Scene-hold extension (%d clips, varied effects): %s",
                 n_clips, arc_prompts[0] if arc_prompts else "")
        job.update(message="Extending single shot across clips (varied environmental effects)")
        # Disable re-anchoring for scene-hold: each clip chains from the previous
        # clip's last frame, which already has the prior atmospheric state baked in.
        # Re-anchoring back to the static source photo resets that state and causes
        # LTX to hallucinate background motion (rain/debris artifacts).
        settings["reanchor_every"] = 0
    else:
        log.warning("[multi] Story arc using built-in fallback: %s", arc_prompts)
        job.update(message="Story arc using default motion phases -- AI planning unavailable")

    if skip_audio:
        job.update(progress=10, message="Story arc ready, waiting for GPU...")
        return

    # -- Music direction ----------------------------------------------------
    music_prompt = settings.get("music_prompt", "")
    if not music_prompt:
        job.update(progress=7, message="Getting music direction...")
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

    # -- Lyrics ------------------------------------------------------------
    if not instrumental and not settings.get("_prepped_lyrics") and music_prompt:
        job.update(progress=9, message="Writing lyrics...")
        try:
            lyrics = analyzer.generate_lyrics(
                llm_router, [], music_prompt, lyric_direction or user_idea,
            )
            if lyrics:
                settings["_prepped_lyrics"] = lyrics
        except Exception as e:
            log.warning("[multi] Lyrics prep failed: %s", e)

    # Release Ollama's model from VRAM now that prep is done.
    # WanGP needs full VRAM when run_multi_pipeline starts.
    # gpu.acquire("wangp") will also do this, but freeing it here
    # means the queue wait time doesn't hold the model unnecessarily.
    _release_ollama_vram()

    job.update(progress=10, message="Story arc ready, waiting for GPU...")


# -- GPU phase -----------------------------------------------------------------

def run_multi_pipeline(job, photo_path, settings):
    """Multi-clip GPU pipeline: N chained clips -> concat -> audio -> merge."""
    from app import get_llm_router, gallery_push
    from features.fun_videos import analyzer, video_generator, audio_generator
    llm_router = get_llm_router()

    def _log(msg):
        log.info(msg)
        display = msg.removeprefix("[info] ").removeprefix("[error] ").removeprefix("[warning] ").removeprefix("[success] ")
        job.update(message=display)

    def _stopped():
        return job.stop_event.is_set()

    # -- Settings ----------------------------------------------------------
    n_clips          = int(settings.get("num_clips", 4))
    clip_dur         = float(settings.get("clip_duration", settings.get("video_duration", 8.0)))
    model_name       = settings.get("model_name", "LTX-2 Dev19B Distilled")
    resolution       = settings.get("resolution", "580p")
    ow               = settings.get("override_width")
    oh               = settings.get("override_height")
    steps            = int(settings.get("video_steps", 30))
    guidance         = float(settings.get("video_guidance", 7.5))
    # LTX-2 Distilled: two-stage compressed schedule. Optimal 4-8 steps; beyond 8 regresses.
    # Guidance above 3.5 over-saturates the compressed schedule.
    # LTX-2 Dev13B: WanGP enforces a hard minimum of 20 steps for ltxv_13B.
    if "ltx" in model_name.lower() and "distilled" in model_name.lower():
        steps    = min(steps, 8)
        guidance = min(guidance, 3.5)
    elif "ltx" in model_name.lower():
        steps = max(steps, 20)
    seed             = int(settings.get("video_seed", -1))
    skip_audio       = settings.get("skip_audio", False)
    instrumental     = settings.get("instrumental", False)
    lyric_dir        = settings.get("lyric_direction", "")
    user_dir         = settings.get("user_direction", "")
    music_prompt     = settings.get("music_prompt", "") or settings.pop("_prepped_music_prompt", "")
    lyrics           = settings.pop("_prepped_lyrics", "")
    story_arc        = settings.pop("_story_arc", [])
    subject_anchor   = settings.pop("_subject_anchor", "")
    director_passes  = max(0, min(2, int(settings.get("director_passes", 0))))
    _is_ltx = "ltx" in model_name.lower()
    motion_style     = settings.get("motion_style") or ("calm" if _is_ltx else "dynamic")

    # reanchor_every: periodically reset the chain start-image back to the source
    # photo to break compounding drift. Default 0 (pure chain, no reset) because
    # snapping back to the source photo every N clips creates visible jump cuts
    # that are far worse than any gradual drift. The Infinite Zoom feature proved
    # this conclusively: reanchor_every=0 + lossless PNG chaining produces smooth,
    # coherent output. The subject_anchor prefix in each prompt handles identity
    # consistency without needing to hard-reset the visual chain.
    # User can override by passing reanchor_every explicitly in settings.
    _user_reanchor = settings.get("reanchor_every")
    if _user_reanchor is not None:
        reanchor_every = int(_user_reanchor)
    else:
        # Compute from audio section boundaries if available, else default to 3.
        # Section boundaries are natural reset points -- the visual cut back to
        # source coincides with a musical section change so it feels intentional.
        _audio_events = settings.get("_audio_events", {})
        _sections = _audio_events.get("sections", []) if _audio_events else []
        if _sections and n_clips >= 2:
            _total_dur = sum(
                float(c.get("duration", 5)) if isinstance(c, dict) else 5
                for c in story_arc
            ) if story_arc else n_clips * 6.0
            _n_in_video = max(1, sum(1 for s in _sections if s.get("start", 0) < _total_dur))
            reanchor_every = max(2, min(5, round(n_clips / max(1, _n_in_video))))
            log.info("[multi] reanchor_every=%d (from %d audio sections)", reanchor_every, len(_sections))
        else:
            reanchor_every = 3

    if not story_arc:
        base = (settings.get("video_prompt", "").strip() + ", ") if settings.get("video_prompt") else ""
        # Same trim compensation applied in run_multi_prep.
        fb_dur = clip_dur / _CHAIN_TRIM_RATIO
        if motion_style == "calm":
            phases = _FALLBACK_PHASES_CALM
        elif motion_style == "narrative":
            phases = _FALLBACK_PHASES_NARRATIVE
        else:
            phases = _FALLBACK_PHASES
        story_arc = [
            {"prompt": base + phases[i % len(phases)], "duration": fb_dur}
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

    # -- Phase 0: Music-first -- generate audio BEFORE clips so we can sync ---
    # ACE-Step produces the track, faster-whisper transcribes it, librosa finds
    # beats and energy peaks, then we snap clip cut points to strong beats and
    # refine each clip's motion prompt with the lyric/energy context.
    # If this phase fails for any reason we fall back to post-clip audio (original flow).
    _audio_phase0_path: str | None = None
    _transcript: list[dict] = []
    _audio_events: dict = {}

    if not skip_audio and music_prompt and not _stopped():
        from core.gpu_orchestrator import gpu as _gp0
        # Skip audio-first if WanGP is already loaded. Evicting it to run ACE-Step
        # then reloading costs 3-8 min (model load), wiping out any sync benefit.
        # Audio-first is only free when WanGP would cold-start anyway.
        _wangp_warm = _gp0.current == "wangp"
        if _wangp_warm:
            log.info("[multi] Phase 0 skipped -- WanGP already warm, audio-first would cost 3-8 min reload")
        else:
            try:
                job.update(progress=10, message="Generating audio for sync analysis...")
                _gp0.acquire("acestep", reason="music-first audio gen before clips")

                planned_dur = sum(
                    float(c.get("duration", clip_dur)) if isinstance(c, dict) else clip_dur
                    for c in story_arc
                )
                audio_dur_p0 = min(planned_dur * _CHAIN_TRIM_RATIO + 4.0, 300.0)

                def _p0_audio_progress(elapsed_s):
                    job.update(
                        progress=10 + min(10, int(elapsed_s) // 5),
                        message=f"Generating audio... {elapsed_s:.0f}s elapsed",
                    )

                _ap0, _aerr0 = audio_generator.generate_audio(
                    prompt=music_prompt,
                    duration=audio_dur_p0,
                    output_dir=str(job_dir),
                    audio_format=settings.get("audio_format", "mp3"),
                    bpm=settings.get("bpm"),
                    steps=int(settings.get("audio_steps", 8)),
                    guidance=float(settings.get("audio_guidance", 7.0)),
                    seed=-1,
                    lyrics=lyrics,
                    instrumental=instrumental,
                    stop_event=job.stop_event,
                    progress_cb=_p0_audio_progress,
                )
                if _ap0 and not _stopped():
                    _audio_phase0_path = _ap0
                    job.update(progress=22, message="Transcribing audio...")
                    from features.fun_videos import audio_analyzer as _aa
                    _transcript = _aa.transcribe_audio(_ap0)

                    job.update(progress=23, message="Analysing beat structure...")
                    _audio_events = _aa.detect_audio_events(_ap0)
                    if _audio_events.get("bpm"):
                        settings["_detected_bpm"] = _audio_events["bpm"]

                    # Snap clip boundaries to strong beats so cuts land on musical moments.
                    # story_arc durations are inflated by 1/TRIM_RATIO for planning;
                    # audio beat times are in real seconds. Deflate to real time before
                    # snapping, then re-inflate so planning compensation is preserved.
                    _real_arc = [
                        dict(c, duration=float(c.get("duration", clip_dur)) * _CHAIN_TRIM_RATIO)
                        if isinstance(c, dict) else c
                        for c in story_arc
                    ]
                    _snapped_real = _aa.snap_durations_to_beats(
                        _real_arc, _audio_events, _audio_events.get("duration", audio_dur_p0)
                    )
                    story_arc = [
                        dict(c, duration=round(float(c.get("duration", clip_dur)) / _CHAIN_TRIM_RATIO, 2))
                        if isinstance(c, dict) else c
                        for c in _snapped_real
                    ]

                    # Build per-clip audio context and refine prompts with lyric hints
                    planned_clip_durs = [
                        float(c.get("duration", clip_dur)) if isinstance(c, dict) else clip_dur
                        for c in story_arc
                    ]
                    clip_audio_ctx = _aa.build_clip_audio_context(
                        _transcript, _audio_events, n_clips, planned_clip_durs,
                        _audio_events.get("duration", audio_dur_p0),
                    )
                    job.update(progress=24, message="Syncing clip prompts to music...")
                    story_arc = _refine_arc_with_audio(
                        llm_router, story_arc, clip_audio_ctx, model_name, motion_style
                    )

                    job.meta["transcript"] = _transcript
                    job.meta["audio_events"] = {
                        "bpm": _audio_events.get("bpm"),
                        "energy_peaks": _audio_events.get("energy_peaks", []),
                        "duration": _audio_events.get("duration"),
                    }
                    job.meta["clip_audio_context"] = clip_audio_ctx
                    log.info("[multi] Music-first done: %d lyric segments, BPM=%.1f, %d clips refined",
                             len(_transcript), _audio_events.get("bpm", 0.0), n_clips)
                else:
                    log.warning("[multi] Phase 0 audio failed (%s) -- will generate audio after clips", _aerr0)
            except Exception as _p0e:
                log.warning("[multi] Phase 0 audio exception (%s) -- will generate audio after clips", _p0e)
                _audio_phase0_path = None

    if _stopped():
        return

    # -- GPU: acquire WanGP exclusively (orchestrator evicts ACE-Step if loaded) -
    from core.gpu_orchestrator import gpu
    gpu.acquire("wangp", reason=f"multi-clip {n_clips} clips")
    try:

        # -- Phase 1: Generate clips sequentially ------------------------------
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

      # Compress the clip-generation progress window when director passes will follow.
      # If audio was pre-generated (Phase 0), clip window starts at 25 instead of 10
      # to leave space for the audio/transcribe phase that already ran.
      _clip_pct_start = 25 if _audio_phase0_path else 10
      _clip_pct_range = (
          {0: 50, 1: 40, 2: 30}.get(director_passes, 30)
          if _audio_phase0_path
          else {0: 65, 1: 45, 2: 35}.get(director_passes, 35)
      )

      clip_paths: list[str] = []
      clip_durations: list[float] = []     # actual probed durations for director frame extraction
      prev_frame_path: str | None = None   # PNG chain frame from previous clip

      # For continuation mode, clip 1 starts from the last frame of the original
      # video (not the uploaded photo), so the AI picks up exactly where it ended.
      video_last_frame   = settings.get("_start_video_last_frame")
      prepend_video_path = settings.get("_prepend_original_video")
      effective_photo_src = video_last_frame or photo_path

      # Pre-process the start image to exact WanGP dimensions
      prepped_photo: str | None = None
      if effective_photo_src and os.path.isfile(effective_photo_src):
          prepped_photo = _prep_photo(effective_photo_src, tw, th, job_dir)

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
          pct_start = _clip_pct_start + int((i / n_clips) * _clip_pct_range)
          pct_end   = _clip_pct_start + int(((i + 1) / n_clips) * _clip_pct_range)

          job.update(progress=pct_start, message=f"Generating clip {clip_num} of {n_clips}...")

          def _video_progress(step, total, _s=pct_start, _e=pct_end, _cn=clip_num):
              pct = _s + int(step / total * (_e - _s)) if total > 0 else _s
              job.update(progress=pct, message=f"Clip {_cn}/{n_clips} -- step {step}/{total}")

          # Prepend subject anchor if the prompt doesn't already open with it.
          # This guards against LLM drift where later clip prompts describe a
          # different-looking subject than the source photo.
          clip_prompt = _strip_camera_moves(clip_prompt)
          if subject_anchor and not clip_prompt.lower().startswith(subject_anchor[:20].lower()):
              clip_prompt = subject_anchor + " " + clip_prompt
          # Clip 1 anchors to source photo; clips 2+ chain from previous last frame.
          clip_start_image = prev_frame_path if prev_frame_path else prepped_photo
          # For chained clips, lock the scene so the model continues from the
          # anchor frame rather than drifting to a new composition. Same pattern
          # as song_video pipeline which has consistent clip continuity.
          if i > 0 and clip_start_image and clip_start_image != prepped_photo:
              clip_prompt = "Exact same location and subject as previous frame, continuous scene. " + clip_prompt
          finalized = _finalize_prompt(clip_prompt, model_name, motion_style)
          clip_out = str(job_dir / f"clip_{i:02d}_{job.id[:6]}.mp4")

          # Chained clips: drop guidance hard so the anchor image dominates.
          # At 3.5 the text prompt can still override the conditioning frame's
          # appearance and "teleport" the character. Empirically:
          #   LTX Distilled (compressed 8-step schedule): 1.5 max -- the
          #     compressed denoising schedule amplifies guidance drift badly.
          #   LTX Dev (40-step schedule): 2.0 max.
          #   Wan I2V: 2.5 max -- less sensitive per step.
          # Clip 1 keeps full guidance because it starts from the source photo
          # (identity is ground truth); clips 2+ use the accumulated chain anchor.
          if i == 0 or not clip_start_image or clip_start_image == prepped_photo:
              effective_guidance = guidance
          elif "distilled" in model_name.lower():
              effective_guidance = min(guidance, 2.8)
          elif "ltx" in model_name.lower():
              effective_guidance = min(guidance, 3.0)
          else:
              effective_guidance = min(guidance, 4.0)

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
                      negative_prompt=video_generator.negative_prompt_for(model_name, motion_style or "dynamic"),
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
          # Expose first clip immediately so the UI can show a preview
          # before the remaining clips finish generating.
          if len(clip_paths) == 1:
              job.meta["first_clip"] = clip_path

          # Trim clip and extract chain anchor for the next clip.
          if i < len(story_arc) - 1:
              frame_out = str(job_dir / f"frame_{i:02d}.png")
              ok, new_dur = _chain_anchor(clip_path, frame_out)
              clip_durations.append(new_dur)
              if not ok:
                  log.warning("[multi] Chain anchor failed for clip %d -- next clip resets to source", i + 1)
                  prev_frame_path = None
              elif reanchor_every > 0 and (i + 1) % reanchor_every == 0:
                  # Periodic re-anchor: clip (i+2) restarts from the source photo.
                  if n_clips > reanchor_every:
                      log.info("[multi] Re-anchoring clip %d back to source photo (every %d)",
                               i + 2, reanchor_every)
                      prev_frame_path = None
                  else:
                      # Blend even when not re-anchoring to kill drift
                      _blend_anchor_with_source(frame_out, prepped_photo)
                      prev_frame_path = frame_out
              else:
                  # Blend anchor with source photo on EVERY transition to prevent
                  # cumulative character appearance drift across clips.
                  _blend_anchor_with_source(frame_out, prepped_photo)
                  prev_frame_path = frame_out
          else:
              clip_durations.append(probe_duration(clip_path) or this_clip_dur)

      if _stopped():
          return

      if not clip_paths:
          raw = _last_error[0] or "No clips generated -- check WanGP is running"
          raise RuntimeError(f"Multi-video failed: {raw}")

      clips_end_pct = _clip_pct_start + _clip_pct_range
      job.update(progress=clips_end_pct, message=f"All {len(clip_paths)} clips done -- generating transitions...")
      job.meta["clips_generated"] = len(clip_paths)
      job.meta["clip_paths"] = clip_paths

      # -- Phase 2: Compile clips with xfade transitions ----------------------
      # A 0.25s crossfade blend at each junction hides the slight luminance/color
      # startup artifact that diffusion models produce on the first frame of each
      # clip regardless of conditioning. Hard cuts make this obvious; the xfade
      # makes it invisible. _concat_with_xfade falls back to hard cut if ffmpeg
      # xfade fails for any reason.
      compile_pct = clips_end_pct + 1
      job.update(progress=compile_pct, message="Compiling clips...")
      concat_path = str(job_dir / f"concat_{job.id[:6]}.mp4")

      # Last clip skips _chain_anchor so it is still in WanGP's native format.
      # Normalize it to CRF-15 libx264 first so xfade sees uniform input format.
      if clip_paths:
          _normalize_clip_for_concat(clip_paths[-1])

      if not _concat_with_xfade(clip_paths, clip_durations, concat_path):
          log.warning("[multi] Concat failed -- using first clip only")
          concat_path = clip_paths[0]

      # -- Director passes (optional) ----------------------------------------
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
                  motion_style=motion_style,
              )

          clip_paths = current_clips
          clip_durations = current_durs
          story_arc = current_arc
          concat_path = current_assembled
          job.meta["clip_paths"] = clip_paths

      # -- Stitch original video + AI continuation ---------------------------
      # In continuation mode, prepend the original video so the output is
      # [original clip] + [AI continuation clips] as one seamless video.
      _model_fps = video_generator.MODELS.get(model_name, {}).get("fps", 25)
      if prepend_video_path and os.path.isfile(prepend_video_path) and not _stopped():
          job.update(message="Stitching original video with AI continuation...")
          norm_path = str(job_dir / "original_normalized.mp4")
          trim_to = settings.get("_prepend_video_trim_seconds")
          if _normalize_video_for_concat(prepend_video_path, norm_path, tw, th, fps=_model_fps, trim_to=trim_to):
              stitched = str(job_dir / f"stitched_{job.id[:6]}.mp4")
              if _concat_clips([norm_path, concat_path], stitched):
                  log.info("[multi] Stitched original + AI continuation -> %s", stitched)
                  concat_path = stitched
              else:
                  log.warning("[multi] Stitch concat failed -- using AI-only output")
          else:
              log.warning("[multi] Could not normalize original video -- using AI-only output")

      job.meta["concat_path"] = concat_path

      # -- Phase 3: Audio ----------------------------------------------------
      if skip_audio:
          job.output = concat_path
          from core.inbox import copy_to_inbox; copy_to_inbox(job.output)
          job.message = f"Multi-video done ({len(clip_paths)} clips, no audio)"
          return

      # If Phase 0 already generated audio, skip regeneration entirely.
      # Otherwise fall back to post-clip audio (original flow).
      audio_path: str | None = _audio_phase0_path
      audio_err: str | None = None
      total_dur = probe_duration(concat_path)  # needed for audio_dur and gallery metadata

      if audio_path:
          job.update(progress=82, message="Audio ready (generated before clips)...")
          if lyrics and not job.meta.get("lyrics"):
              job.meta["lyrics"] = lyrics
          # Transcribe if we haven't already (Phase 0 should have done this, but guard it)
          if not _transcript and not instrumental:
              try:
                  from features.fun_videos import audio_analyzer as _aa
                  _tx = _aa.transcribe_audio(audio_path)
                  if _tx:
                      job.meta["transcript"] = _tx
              except Exception:
                  pass
      else:
          # Fallback: generate audio after clips (Phase 0 failed or was skipped)
          # -- GPU: hand off from WanGP to ACE-Step (orchestrator evicts WanGP) --
          gpu.acquire("acestep", reason="music gen after WanGP phase (Phase 0 skipped)")

          if not music_prompt:
              job.update(progress=78, message="Analysing video for music direction...")
              try:
                  frames = _sample_music_frames(concat_path, llm_router)
                  if frames:
                      result = analyzer.generate_music_prompt(llm_router, frames, user_dir)
                      music_prompt = result.get("music_prompt", "dark cabaret, accordion, upright bass, brushed snare, smoky bistro atmosphere")
                      if not settings.get("bpm") and result.get("bpm"):
                          settings["bpm"] = result["bpm"]
              except Exception as e:
                  log.warning("[multi] Post-video music analysis failed: %s", e)
              if not music_prompt:
                  music_prompt = "dark cabaret, accordion, upright bass, brushed snare, smoky bistro atmosphere"
          else:
              job.update(progress=78, message="Using pre-generated music direction...")

          if not instrumental and not lyrics:
              job.update(progress=80, message="Writing lyrics...")
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

          if lyrics:
              job.meta["lyrics"] = lyrics
          job.update(progress=82, message="Generating audio for full story...")

          audio_dur = min(total_dur + 2.0, 300.0) if total_dur > 0 else float(sum(
              d.get("duration", clip_dur) if isinstance(d, dict) else clip_dur
              for d in story_arc
          ) + 2.0)

          def _audio_progress(elapsed_s):
              job.update(
                  progress=82 + min(8, int(elapsed_s) // 10),
                  message=f"Generating audio... {elapsed_s:.0f}s elapsed",
              )

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

          if audio_path and not instrumental:
              # Transcribe the post-clip fallback audio so metadata matches
              try:
                  from features.fun_videos import audio_analyzer as _aa
                  _tx = _aa.transcribe_audio(audio_path)
                  if _tx:
                      job.meta["transcript"] = _tx
              except Exception:
                  pass

      if _stopped():
          return

      if not audio_path:
          _log(f"[warning] Audio failed: {audio_err} -- video saved without audio")
          job.output = concat_path
          from core.inbox import copy_to_inbox; copy_to_inbox(job.output)
          job.message = f"Video saved ({len(clip_paths)} clips, no audio -- ACE-Step failed: {audio_err})"
          return

      # -- Phase 4: Merge ----------------------------------------------------
      job.update(progress=92, message="Merging video + audio...")

      model_tag  = model_name.split()[0].lower()
      final_path = str(job_dir / f"multi_{model_tag}_{time.strftime('%H%M%S')}.mp4")

      merged = video_generator.merge_video_audio(concat_path, audio_path, final_path, log_fn=_log)

      if merged:
          # -- Phase 5: Upscale (optional) ---------------------------------------
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

          # Clean up ALL intermediates -- only the merged file is needed.
          # Director re-shoots leave orphaned clip_*_p{N}_*.mp4, concat_p{N}_*.mp4,
          # frame_p{N}_*.png, and review-frame rv_*.jpg files behind that the
          # narrow clip_paths sweep would miss. Glob each pattern instead.
          merged_abs = os.path.abspath(merged)
          for pattern in ("clip_*.mp4", "concat_*.mp4", "frame_*.png", "rv_*.jpg"):
              for stale in job_dir.glob(pattern):
                  try:
                      if os.path.abspath(str(stale)) == merged_abs:
                          continue
                      stale.unlink()
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
    finally:
        # Orchestrator owns Forge state -- next SD-prompts request will acquire
        # Forge and trigger a reload. No need to manually restore here.
        pass
