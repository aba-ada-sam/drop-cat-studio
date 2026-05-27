"""Song Video pipeline: audio file -> N chained video clips -> merge with original audio.

Beat-synced architecture:
  1. Audio analyzer extracts beat times + per-clip target peak position
  2. LLM writes a single story arc, one prompt per clip, each with a clear
     visual climax (the LLM does NOT try to time the climax -- it just makes
     sure each clip has one)
  3. WanGP renders each clip; we then run frame-difference motion analysis
     to find where the visual climax actually landed
  4. ffmpeg piecewise speed-ramp warps the clip so the natural climax slides
     onto the audio's beat timestamp. Clip duration is preserved.
  5. Hard-cut concat -- boundaries chain via identical first/last frames

No ACE-Step involved. The user's uploaded song is the audio track.
"""
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from core.ffmpeg_utils import probe_duration
from core.llm_client import TIER_BALANCED, TIER_FAST, encode_image_b64, parse_json_response
from features.fun_videos.pipeline import _prep_photo, _finalize_prompt
from features.fun_videos.multi_pipeline import _concat_clips, _concat_with_xfade
from features.song_video.motion_analyzer import align_clip_to_beat

log = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"


def _generate_forge_keyframes(
    source_photo: str,
    n_keyframes: int,
    subject_anchor: str,
    output_dir: Path,
    width: int = 1032,
    height: int = 580,
) -> list[str]:
    """Generate N+1 high-quality keyframe images using Forge img2img.

    Each keyframe is generated independently from the source photo at very low
    denoising (0.10), so character quality is maintained across all frames with
    no compounding degradation. Different seeds provide subtle natural variation.

    These are used as start_image + end_image for each LTX-2 clip:
      clip[i]: start=keyframe[i], end=keyframe[i+1]

    This guarantees smooth visual continuity at every clip boundary because the
    clip ends approaching keyframe[i+1], and the next clip starts from keyframe[i+1].

    Falls back gracefully if Forge is unavailable.
    """
    try:
        from services.forge_client import img2img, forge_alive
        if not forge_alive():
            log.info("[song-video] Forge not available -- skipping keyframe pre-generation")
            return []
    except Exception as e:
        log.debug("[song-video] Forge check failed: %s", e)
        return []

    try:
        from core.llm_client import encode_image_b64
        source_b64 = encode_image_b64(source_photo)
        if not source_b64:
            return []
    except Exception:
        return []

    prompt = (subject_anchor or "photorealistic portrait, high quality") + \
             ", natural lighting, consistent character appearance"
    neg    = "ugly, deformed, blurry, low quality, artifacts, noise, anime, cartoon"

    keyframes: list[str] = [source_photo]
    import random
    rng = random.Random(42)

    log.info("[song-video] Generating %d Forge keyframes for seamless transitions...", n_keyframes)
    for i in range(n_keyframes - 1):
        out_path = str(output_dir / f"keyframe_{i+1:02d}.png")
        seed = rng.randint(1, 2**31)
        try:
            result = img2img(
                init_image_b64=source_b64,
                prompt=prompt,
                negative_prompt=neg,
                denoising_strength=0.10,
                width=width,
                height=height,
                steps=8,
                cfg_scale=4.0,
                seed=seed,
                save_images=False,
            )
            if result.get("error") or not result.get("images"):
                log.warning("[song-video] Forge keyframe %d failed: %s", i + 1, result.get("error"))
                break
            import base64 as _b64
            img_data = result["images"][0]
            if "," in img_data:
                img_data = img_data.split(",", 1)[1]
            Path(out_path).write_bytes(_b64.b64decode(img_data))
            keyframes.append(out_path)
            log.debug("[song-video] Keyframe %d/%d generated", i + 1, n_keyframes - 1)
        except Exception as e:
            log.warning("[song-video] Keyframe %d exception: %s -- stopping early", i + 1, e)
            break

    if len(keyframes) < 2:
        log.warning("[song-video] Too few keyframes (%d) -- falling back to chain approach", len(keyframes))
        return []

    log.info("[song-video] Generated %d/%d keyframes via Forge", len(keyframes), n_keyframes)
    return keyframes



def _extract_last_frame(video_path: str, out_path: str) -> str | None:
    """Extract the actual last frame of a video as a lossless PNG.

    Probes the duration first then seeks to 2 frames before the end, so the
    extracted frame is the true final frame rather than an arbitrary point
    0.5s before end. This matters for seamless hard-cut chaining -- clip N+1
    must start from the exact same frame that clip N ended on.
    """
    dur = probe_duration(video_path)
    if dur and dur > 0.1:
        seek = max(0.0, dur - 0.08)  # 2 frames before end at 25 fps
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{seek:.4f}", "-i", video_path,
             "-frames:v", "1", out_path],
            capture_output=True, timeout=30,
        )
    else:
        r = subprocess.run(
            ["ffmpeg", "-y", "-sseof", "-0.1", "-i", video_path,
             "-frames:v", "1", out_path],
            capture_output=True, timeout=30,
        )
    return out_path if (r.returncode == 0 and Path(out_path).exists()) else None


# -- Subject anchor extraction -------------------------------------------------

def _extract_subject_anchor(photo_path: str, llm_router) -> str:
    """Vision call to get a 12-15 word subject description prepended to every prompt.

    Runs concurrently with beat analysis in run_song_prep so it adds ~0 wall time.
    Same pattern as multi_pipeline._generate_subject_anchor.
    """
    if not photo_path or not os.path.isfile(photo_path):
        return ""
    try:
        b64 = encode_image_b64(photo_path)
        if not b64:
            return ""
        raw = llm_router.route_vision(
            "Describe the main subject's APPEARANCE in 15-20 words. "
            "The subject may be human, animal, creature, object, or fantasy -- describe whatever is there. "
            "Focus on: color, texture, material, shape, distinguishing features. "
            "Output ONLY the description, no preamble like 'The subject is' or 'I see'. "
            "Examples: "
            "'Dusty rose skull, exposed cheekbones, dark hollow eye sockets, root tendrils at jaw.' "
            "'Brown teddy bear, worn felt nose, dark bead eyes, soft fluffy ears.' "
            "'Red-haired woman, blue denim jacket, pale freckled skin, hazel eyes.'",
            [b64], tier=TIER_FAST, max_tokens=90,
        )
        import re as _re
        anchor = raw.strip()
        anchor = _re.sub(r'^#+\s*[^\n]*\n+', '', anchor)
        anchor = _re.sub(r'\*\*([^*]+)\*\*', r'\1', anchor)
        anchor = _re.sub(r'\*([^*]+)\*', r'\1', anchor)
        anchor = anchor.strip().strip('"').strip("'").split('\n')[0].strip()
        # Reject unhelpful non-description responses
        bad_starts = ("i don't see", "i cannot", "i can't", "there is no", "no person",
                      "i see no", "i'm unable", "i am unable", "the image shows no")
        if any(anchor.lower().startswith(b) for b in bad_starts):
            log.warning("[song-video] Subject anchor rejected unhelpful response: %r", anchor[:80])
            return ""
        if anchor and not anchor.endswith("."):
            anchor += "."
        return anchor
    except Exception as e:
        log.warning("[song-video] Subject anchor extraction failed (non-fatal): %s", e)
        return ""


# -- Story arc generation ------------------------------------------------------

_SONG_ARC_SYSTEM = """\
You write image-to-video motion prompts for a music video.
The AI model SEES the reference image -- do NOT describe what things look like.
Describe ONLY what CHANGES: what moves, where it starts, where it ends.

IDENTITY LOCK (mandatory every prompt):
Start with 8-12 words of the subject's exact visual markers from the photo.
Example: "pale elf, pointed ears, purple jacket, blue jeans"
Without this the model generates a different character each clip.

LOCATION LOCK (mandatory every prompt):
Include 6-10 words of the original setting from the photo.
Example: "among large purple mushrooms, wooden fence background"
Without this the background becomes fire/electricity/generic hallucination.

BEAT ALIGNMENT (critical):
Each clip includes "PEAK AT Xs" -- this is the beat hit in the music.
Your subject MUST reach their peak gesture/expression AT exactly that moment.
Structure every prompt: build-up BEFORE the peak, peak AT the moment, resolution AFTER.

VARIETY RULE (mandatory):
Every clip must use a DIFFERENT motion from the clips before and after it.
NEVER repeat the same action twice in a row. Arm lifts, head turns, weight shifts,
reaching, leaning, swaying, shaking, nodding, tilting -- vary them.
If clip 3 is a head turn, clip 4 must be something else entirely.

Motion menu (pick a DIFFERENT one each clip):
  Head/face: head turns left/right, chin drops/lifts, jaw opens/closes, eyes close, face tilts
  Torso: body leans forward/back/sideways, chest expands, shoulder rolls, spine straightens
  Arms: one arm rises/lowers, hand reaches out/retracts, fingers spread/close, wrist rotates
  Weight: weight shifts hip left/right, slight crouch/rise, swaying, rocking
  Fabric/texture: clothing stirs, hair moves, fabric ripples at one edge

Match energy to song section:
  LOW energy (verse/intro): single small motion -- chin drops 5cm, one shoulder settles
  MED energy (pre-chorus): medium motion -- torso leans, one arm lifts partway
  HIGH energy (chorus/drop): bold single action -- full lean, sharp head snap, arm fully extended

BANNED (causes artifacts):
  anime, cartoon, 2D, ethereal, mystical, blazing, transcendent
  zoom, pan, push, pull, dolly, tilt (camera moves)
  dust, sparks, smoke, fog, bokeh, confetti
  multiple simultaneous actions, walking, dancing

Return ONLY valid JSON:
{"clips": [{"prompt": "...", "duration": 7}, {"prompt": "...", "duration": 8}]}
Duration: seconds per clip (5-10).\
"""


def _generate_song_arc(
    llm_router,
    n_clips: int,
    analysis: dict,
    user_idea: str,
    photo_path: str | None,
    variety_theme: str = "",
    lyrics_text: str = "",
    clip_durations: list | None = None,
    beat_positions: list | None = None,
) -> list[str]:
    """Generate N motion prompts that follow a single story across the song.

    Passes beat timing (peak second within each clip) and per-clip lyrics
    so the LLM can instruct the model to peak at the exact musical moment.
    """
    clip_labels = analysis.get("clip_energy_labels", [])
    bpm    = analysis.get("bpm")
    key    = analysis.get("key", "")
    mode   = analysis.get("mode", "")
    mood   = analysis.get("mood", "")
    clip_durations  = clip_durations or []
    beat_positions  = beat_positions or []

    # Map lyrics lines to clip windows proportionally.
    # Divides the full lyrics text into N roughly equal sections so each clip
    # gets the lyrical content that plays during its time window.
    lyrics_lines = [ln.strip() for ln in lyrics_text.splitlines() if ln.strip()] if lyrics_text else []
    def _clip_lyrics(i: int) -> str:
        if not lyrics_lines or not n_clips:
            return ""
        start = int(i * len(lyrics_lines) / n_clips)
        end   = int((i + 1) * len(lyrics_lines) / n_clips)
        snippet = " / ".join(lyrics_lines[start:end])
        return snippet[:120] if snippet else ""

    clip_hints = []
    for i in range(n_clips):
        label = clip_labels[i] if i < len(clip_labels) else "MED"
        dur   = float(clip_durations[i]) if i < len(clip_durations) else 8.0
        bpos  = float(beat_positions[i])  if i < len(beat_positions)  else 0.5
        peak_sec = round(bpos * dur, 1)
        lyrics_snip = _clip_lyrics(i)
        lyric_part = f" | lyrics: \"{lyrics_snip}\"" if lyrics_snip else ""
        clip_hints.append(
            f"Clip {i + 1:02d} ({dur:.0f}s | {label} energy | PEAK AT {peak_sec}s){lyric_part}"
        )

    energy_text = "\n".join(clip_hints)
    key_str  = f"{key} {mode}".strip() if key else ""
    bpm_str  = f"{bpm} BPM" if bpm else ""
    song_desc = ", ".join(filter(None, [key_str, bpm_str, mood]))

    story_direction = (user_idea or "").strip() or "a music video that visually matches the song's mood and energy"
    style_line = f"Visual style / aesthetic: {variety_theme}\n" if variety_theme else ""

    user_msg = (
        f"Song character: {song_desc or 'dynamic track'}\n"
        f"Story direction: {story_direction}\n"
        f"{style_line}"
        f"\nPer-clip beat map ({n_clips} clips) -- PEAK AT = exact second for visual climax:\n"
        f"{energy_text}\n\n"
        f"Generate exactly {n_clips} motion prompts. "
        f"CRITICAL: use a completely DIFFERENT motion type for every single clip -- "
        f"never the same action twice in a row. Choose from the motion menu in the system prompt. "
        f"Each prompt MUST build to its peak at the stated second, then resolve. "
        f"Use the lyrics as emotional/thematic context for what kind of action fits."
    )

    try:
        frames = []
        if photo_path and os.path.isfile(photo_path):
            b64 = encode_image_b64(photo_path)
            if b64:
                frames = [b64]
        # Budget ~150 tokens per clip (50-word prompt ~ 70 tokens + JSON overhead).
        # 3000 was too small for 27+ clips and caused truncated responses, triggering
        # the last-prompt-repeated fallback which made clips look identical.
        max_tok = max(6000, n_clips * 150)
        if frames:
            text = llm_router.route_vision(
                user_msg, frames,
                tier=TIER_BALANCED, system=_SONG_ARC_SYSTEM, max_tokens=max_tok,
            )
        else:
            text = llm_router.route(
                [{"role": "user", "content": user_msg}],
                tier=TIER_BALANCED, system=_SONG_ARC_SYSTEM, max_tokens=max_tok,
            )
        data = parse_json_response(text)
        if data is None:
            log.warning("[song-video] Story arc: LLM returned no parseable JSON -- raw: %.200s", text)
            raise ValueError("No JSON in LLM response")
        clips = data.get("clips", [])
        if isinstance(clips, list) and clips:
            # Preserve dict format {prompt, duration} if LLM returned it;
            # otherwise wrap plain strings into dicts with default duration.
            result = []
            for c in clips[:n_clips]:
                if isinstance(c, dict) and c.get("prompt"):
                    result.append({"prompt": str(c["prompt"]), "duration": float(c.get("duration", 7))})
                elif isinstance(c, str) and c.strip():
                    result.append({"prompt": c.strip(), "duration": 7.0})
            # Pad if LLM returned fewer clips than needed
            src = len(result)
            while len(result) < n_clips and src > 0:
                result.append(dict(result[len(result) % src]))
            log.info("[song-video] Story arc: %d prompts from LLM, padded to %d", src, n_clips)
            return result
    except Exception as e:
        log.warning("[song-video] Story arc LLM call failed: %s", e)

    base = user_idea or "Subject in original scene, natural physical movement"
    return [{"prompt": base, "duration": 7.0}] * n_clips


def _merge_video_audio_trim(
    video_path: str,
    audio_path: str,
    out_path: str,
    audio_duration: float,
    pad_before: float = 0.0,
) -> str | None:
    """Merge video + audio, looping video if needed. pad_before delays audio onset."""
    video_dur = probe_duration(video_path) or 0.0
    true_audio_dur = probe_duration(audio_path) or audio_duration
    target_dur = max(true_audio_dur + pad_before, audio_duration)

    need_loop = video_dur > 0 and video_dur < target_dur * 0.98

    # Build audio filter: delay by pad_before ms if requested
    audio_filter = f"adelay={int(pad_before * 1000)}|{int(pad_before * 1000)},apad" if pad_before > 0 else "apad"

    if need_loop:
        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", video_path,
            "-i", audio_path,
            "-map", "0:v",
            "-filter_complex", f"[1:a]{audio_filter}[a]",
            "-map", "[a]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "15",
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{target_dur:.3f}",
            "-movflags", "+faststart",
            out_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-map", "0:v",
            "-filter_complex", f"[1:a]{audio_filter}[a]",
            "-map", "[a]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{target_dur:.3f}",
            "-movflags", "+faststart",
            out_path,
        ]

    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600)
        if r.returncode == 0 and Path(out_path).exists():
            return out_path
        log.error("[song-video] merge failed:\n%s", r.stderr.decode(errors="replace")[-2000:])
    except Exception as e:
        log.error("[song-video] merge exception: %s", e)
    return None


# -- Prep phase ----------------------------------------------------------------

def run_song_prep(job, photo_path, settings):
    """Phase 0: beat plan + lyric detection + LLM story arc. CPU only, no GPU."""
    from app import get_llm_router
    llm_router = get_llm_router()

    import concurrent.futures

    n_clips       = int(settings.get("num_clips", 10))
    clip_dur      = float(settings.get("clip_duration", 8.0))
    user_idea     = settings.get("video_prompt", "") or settings.get("user_direction", "")
    variety_theme = settings.get("variety_theme", "")
    analysis      = settings.get("audio_analysis", {})
    audio_path    = settings.get("audio_path", "")
    lyrics_text   = (settings.get("lyrics_text") or "").strip()

    # Prep strategy: I/O-bound API calls run concurrently with CPU work.
    # CPU-heavy tasks (librosa x2, whisper) run sequentially so each gets full cores --
    # running them simultaneously saturates CPU and slows all three.
    job.meta["stage"] = "analyzing"
    job.meta["clips_total"] = n_clips
    job.update(progress=2, message="Analysing beat structure and detecting lyrics...")
    from features.song_video.audio_analyzer import compute_clip_plan, _transcribe_lyrics
    from features.fun_videos.audio_analyzer import detect_audio_events

    # Fire Anthropic vision call in background (pure I/O, no CPU competition).
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as api_pool:
        fut_anchor = api_pool.submit(_extract_subject_anchor, photo_path, llm_router) if photo_path else None

        # CPU tasks run sequentially -- each gets full cores.
        clip_durations, beat_positions = compute_clip_plan(
            audio_path, n_clips, clip_dur, max(float(clip_dur), 10.0)
        )
        audio_events = detect_audio_events(audio_path) if os.path.isfile(audio_path) else {}
        if not lyrics_text and os.path.isfile(audio_path):
            detected = _transcribe_lyrics(audio_path)
            if detected:
                lyrics_text = detected
                log.info("[song-video] Auto-detected %d chars of lyrics", len(lyrics_text))

        subject_anchor = fut_anchor.result() if fut_anchor else ""
    if subject_anchor:
        log.info("[song-video] Subject anchor: %s", subject_anchor[:80])

    # Guard: if the song is shorter than n_clips * min_dur, _place_boundaries
    # collapses trailing boundaries to total_dur, producing zero-duration clips.
    # ffmpeg -t 0 then writes an empty file and all subsequent clips fail.
    clip_durations = [max(8.0, min(10.0, d)) for d in clip_durations]
    settings["_clip_durations"] = clip_durations
    settings["_beat_positions"] = beat_positions
    log.info("[song-video] Clip durations (beat-aligned): %s", clip_durations)
    log.info("[song-video] Beat positions per clip: %s", beat_positions)

    # Compute audio start time for each clip's conditioning slice.
    # Each xfade overlaps consecutive clips by _SONG_XFADE_DUR seconds, shortening
    # the output timeline. Clip i appears in the final video at:
    #   T_i = pad_before + sum(d[0..i-1]) - i * xfade_dur
    # Without this correction, later clips get conditioned on audio that is up to
    # (n_clips-1) * xfade_dur seconds ahead of where they actually appear -- at 27
    # clips and 0.75s xfade that's ~19 seconds of accumulated sync drift.
    _SONG_XFADE_DUR = 0.12   # must match the fade_dur passed to _concat_with_xfade below
    # 0.12s = 3 frames at 24fps -- fast enough to read as a soft cut, not a dissolve.
    # Longer fades (0.5-0.75s) create visible double-exposure "sludge" where the brain
    # perceives two overlaid images. At 0.12s the blend is subliminal.
    _start_t = float(settings.get("pad_before", 1.0))
    _clip_start_times: list[float] = []
    for _idx, _d in enumerate(clip_durations):
        corrected = max(0.0, _start_t - _idx * _SONG_XFADE_DUR)
        _clip_start_times.append(corrected)
        _start_t += float(_d)
    settings["_clip_start_times"] = _clip_start_times
    settings["_song_xfade_dur"]   = _SONG_XFADE_DUR

    # Pre-convert user audio to stereo 44100 Hz WAV for per-clip conditioning.
    # WAV is what WanGP's LTX-2 audio conditioning expects; the user's file may be
    # MPEG/MP3/AAC. This runs once in prep so the GPU phase only does cheap slicing.
    _audio_wav: str | None = None
    if bool(settings.get("lip_sync", True)) and os.path.isfile(audio_path):
        import tempfile as _tf
        _wav_dir = Path(_tf.gettempdir()) / "dcs_song_audio"
        _wav_dir.mkdir(exist_ok=True)
        # Include job ID in filename so concurrent satellite jobs on the same
        # song don't overwrite each other's WAV during simultaneous prep phases.
        _wav_path = str(_wav_dir / f"{Path(audio_path).stem}_{job.id[:8]}.wav")
        _r = subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path,
             "-vn", "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2", _wav_path],
            capture_output=True, timeout=60,
        )
        if _r.returncode == 0 and os.path.isfile(_wav_path) and os.path.getsize(_wav_path) > 1024:
            _audio_wav = _wav_path
            log.info("[song-video] Audio converted to WAV for lip sync: %s", _wav_path)
        else:
            log.warning("[song-video] Audio WAV conversion failed or empty -- lip sync disabled")
    settings["_audio_wav"] = _audio_wav

    settings["_subject_anchor"] = subject_anchor

    job.meta["stage"] = "planning"
    job.update(progress=4, message="Planning music video story arc...")
    try:
        arc = _generate_song_arc(llm_router, n_clips, analysis, user_idea, photo_path, variety_theme, lyrics_text,
                                  clip_durations=clip_durations, beat_positions=beat_positions)
        settings["_story_arc"] = arc
        log.info("[song-video] Story arc (%d clips) generated", n_clips)
    except Exception as e:
        log.warning("[song-video] Story arc failed: %s", e)
        arc = [user_idea or "Subject erupts into motion"] * n_clips
        settings["_story_arc"] = arc

    # Snap story arc clip boundaries to strong beats/energy peaks
    if audio_events and arc:
        try:
            from features.fun_videos.audio_analyzer import snap_durations_to_beats
            audio_dur_snap = float(analysis.get("duration") or probe_duration(audio_path) or 0)
            arc = snap_durations_to_beats(arc, audio_events, audio_dur_snap, snap_window=2.0)
            settings["_story_arc"] = arc
            log.info("[song-video] Story arc durations snapped to beats")
        except Exception as e:
            log.warning("[song-video] Beat-snap failed (non-fatal): %s", e)

    reanchor_every = 0
    settings["_reanchor_every"] = reanchor_every

    # Forge keyframe generation removed -- chain-frame anchoring handles identity.
    # Forge is not required and may not be running. The _keyframes key is kept
    # for backwards compatibility but is always empty now.
    settings["_keyframes"] = []

    job.meta["stage"] = "waiting-gpu"
    job.update(progress=10, message="Story arc ready, waiting for GPU...")


# -- GPU phase -----------------------------------------------------------------

def run_song_pipeline(job, photo_path, settings):
    """Song-video GPU pipeline: N chained clips -> concat -> merge with user's audio."""
    from features.fun_videos import video_generator

    # -- Settings ----------------------------------------------------------
    n_clips        = int(settings.get("num_clips", 10))
    clip_dur       = float(settings.get("clip_duration", 8.0))
    clip_durations     = settings.pop("_clip_durations", None) or [clip_dur] * n_clips
    beat_positions     = settings.pop("_beat_positions", None) or [0.5] * n_clips
    clip_start_times   = settings.pop("_clip_start_times", None) or []
    audio_wav          = settings.pop("_audio_wav", None)  # pre-converted WAV for lip sync
    model_name     = settings.get("model_name", "LTX-2 Dev19B Distilled")
    resolution    = settings.get("resolution", "580p")
    ow            = settings.get("override_width")
    oh            = settings.get("override_height")
    steps         = int(settings.get("video_steps", 30))
    # LTX Distilled sweet spot is 8 steps -- quality doesn't improve beyond that,
    # it only costs time. Cap it here so any settings path lands at the right value.
    if "distilled" in model_name.lower() and "ltx" in model_name.lower():
        steps = min(steps, 8)
    guidance      = float(settings.get("video_guidance", 7.5))
    seed          = int(settings.get("video_seed", -1))
    audio_path    = settings.get("audio_path", "")   # user's uploaded song
    audio_dur     = float(settings.get("audio_duration", 0.0))
    pad_before    = float(settings.get("pad_before", 0.0))
    story_arc      = settings.pop("_story_arc", [])
    subject_anchor = settings.pop("_subject_anchor", "")
    reanchor_every = int(settings.pop("_reanchor_every", 3))
    keyframes      = settings.pop("_keyframes", [])  # Forge-generated start/end frames

    if not story_arc:
        story_arc = [settings.get("video_prompt", "") or "Subject erupts into motion"] * n_clips
    if not audio_path or not os.path.isfile(audio_path):
        raise RuntimeError("Audio file not found -- please re-upload the song")

    ts      = time.strftime("%Y-%m-%d")
    slug    = Path(photo_path).stem[:14].replace(" ", "_") if photo_path else "songvid"
    job_dir = OUTPUT_DIR / ts / f"songvid_{slug}_{job.id}"
    job_dir.mkdir(parents=True, exist_ok=True)

    # Resolution strategy:
    # - Lip sync ON + audio WAV available: generate at 640x360 so audio tokens
    #   fit in LTX-2's context window, then upscale each clip to 580p before concat.
    #   At 580p the audio token budget is exceeded and WanGP silently drops audio
    #   conditioning -- 360p is required for any audio-driven motion to work.
    # - Lip sync OFF or no audio WAV: native 580p, audio conditioning unavailable.
    # - Explicit override: honour ow/oh directly.
    # Forge keyframe dependency removed: chain-frame anchoring alone is sufficient
    # to prevent identity drift. Forge is not required.
    _lip_sync_res_active = (
        bool(settings.get("lip_sync", True)) and
        bool(audio_wav)
    )
    if ow and oh:
        tw, th = int(ow), int(oh)
    elif _lip_sync_res_active:
        tw, th = 640, 360
        log.info("[song-video] Lip sync: generating at 640x360 (audio context fits), will upscale each clip to 580p")
    else:
        _native = video_generator.MODELS.get(model_name, {}).get("res") or (1032, 580)
        tw, th = _native

    if photo_path and os.path.isfile(photo_path):
        shutil.copy2(photo_path, job_dir / f"source{Path(photo_path).suffix}")

    # -- GPU: acquire WanGP exclusively (orchestrator evicts everything else)
    from core.gpu_orchestrator import gpu
    gpu.acquire("wangp", reason=f"song-video {n_clips} clips")
    try:
        _do_song_gpu_phase(
            job, photo_path, settings, job_dir,
            n_clips, clip_durations, beat_positions, model_name,
            resolution, ow, oh, tw, th, steps, guidance, seed,
            audio_path, audio_dur, story_arc, clip_dur, subject_anchor,
            reanchor_every=reanchor_every, pad_before=pad_before,
            clip_start_times=clip_start_times,
            audio_wav=audio_wav,
            keyframes=keyframes,
            lip_sync_res_active=_lip_sync_res_active,
        )
    finally:
        _cleanup_gpu_phase_temps(job_dir, audio_wav)
    # Orchestrator keeps WanGP loaded; next acquire of a different service evicts.


def _do_song_gpu_phase(
    job, photo_path, settings, job_dir,
    n_clips, clip_durations, beat_positions, model_name,
    resolution, ow, oh, tw, th, steps, guidance, seed,
    audio_path, audio_dur, story_arc, clip_dur, subject_anchor,
    reanchor_every=3, pad_before=0.0, clip_start_times=None, audio_wav=None,
    keyframes=None, lip_sync_res_active=False,
):
    from app import gallery_push
    from features.fun_videos import video_generator

    def _log(msg):
        log.info(msg)
        display = msg.removeprefix("[info] ").removeprefix("[error] ").removeprefix("[warning] ").removeprefix("[success] ")
        job.update(message=display)

    def _stopped():
        return job.stop_event.is_set()

    # -- Phase 1: Generate clips -------------------------------------------
    clip_paths: list[str] = []

    prepped_photo: str | None = None
    if photo_path and os.path.isfile(photo_path):
        prepped_photo = _prep_photo(photo_path, tw, th, job_dir)

    # Prep keyframes for use as start+end images (Forge-generated, if available).
    # When keyframes exist: clip[i] starts at keyframe[i] and is guided to end at
    # keyframe[i+1], guaranteeing seamless transitions without character degradation.
    # When not available: fall back to chain frame approach.
    _kf = keyframes or []
    _use_keyframes = len(_kf) >= n_clips + 1
    if _use_keyframes:
        log.info("[song-video] Using %d Forge keyframes as start/end anchors", len(_kf))
    else:
        log.info("[song-video] No keyframes available -- using chain frame approach")

    _last_error: list[str | None] = [None]
    _chain_frame: str | None = None   # last frame of previous clip -> first frame of next
    _clip_secs: list[float] = []      # per-clip wall-clock times for ETA

    # Lip sync: slice the pre-converted WAV per clip and pass as audio_source.
    # Uses the same -c:a copy slice approach as the Zoom pipeline (known to work).
    # Only enabled when user provided audio that converted successfully in prep.
    _clip_start_times = clip_start_times or []
    _lip_sync = bool(settings.get("lip_sync", True)) and bool(audio_wav) and len(_clip_start_times) == n_clips
    _audio_slices_dir = job_dir / "audio_slices"

    # Pre-extract ALL audio slices before the clip generation loop starts.
    # This runs once upfront so WanGP never waits for an ffmpeg subprocess
    # between clips. Each slice: corrected start time + clip duration from WAV.
    _audio_slices: list[str | None] = [None] * n_clips
    if _lip_sync:
        _audio_slices_dir.mkdir(exist_ok=True)
        log.info("[song-video] Lip sync ON -- pre-extracting %d audio slices", n_clips)
        for _si, (_st, _arc) in enumerate(zip(_clip_start_times, story_arc)):
            _sdur = float(_arc.get("duration", clip_dur) if isinstance(_arc, dict) else clip_dur)
            _sp = str(_audio_slices_dir / f"slice_{_si:02d}.wav")
            _sr = subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{_st:.4f}", "-t", f"{_sdur:.4f}",
                 "-i", audio_wav, "-c:a", "copy", _sp],
                capture_output=True, timeout=30,
            )
            if _sr.returncode == 0 and Path(_sp).exists():
                _audio_slices[_si] = _sp
        log.info("[song-video] Audio slices ready: %d/%d", sum(1 for s in _audio_slices if s), n_clips)
    elif settings.get("lip_sync") and not audio_wav:
        log.warning("[song-video] Lip sync requested but audio WAV conversion failed -- skipping")
    elif settings.get("lip_sync") and len(_clip_start_times) != n_clips:
        log.warning("[song-video] Lip sync skipped -- clip_start_times length %d != n_clips %d", len(_clip_start_times), n_clips)

    for i, _arc_entry in enumerate(story_arc):
        clip_prompt = _arc_entry.get("prompt", "") if isinstance(_arc_entry, dict) else str(_arc_entry)
        if _stopped():
            break

        clip_num  = i + 1
        pct_start = 10 + int((i / n_clips) * 68)
        pct_end   = 10 + int(((i + 1) / n_clips) * 68)

        eta_str = ""
        if _clip_secs:
            avg = sum(_clip_secs) / len(_clip_secs)
            rem = (n_clips - i) * avg
            eta_str = f" -- ~{int(rem // 60)}m {int(rem % 60):02d}s left"

        job.update(progress=pct_start, message=f"Clip {clip_num}/{n_clips}{eta_str}...")
        _clip_t0 = time.time()

        def _video_progress(step, total, _s=pct_start, _e=pct_end, _cn=clip_num, _et=eta_str):
            pct = _s + int(step / total * (_e - _s)) if total > 0 else _s
            job.update(progress=pct, message=f"Clip {_cn}/{n_clips} -- step {step}/{total}{_et}")

        # Worker URL: local by default, satellite if batch runner requested it.
        _worker_url = settings.get("wangp_worker_url") or None

        # Clip chaining: clips 2+ start from the last frame of the previous clip,
        # not the source photo. This gives narrative progression -- each clip
        # continues from where the previous one ended. reanchor_every resets to
        # source periodically (at section boundaries) to prevent quality drift.
        # _chain_frame is None for clip 0 and after each reanchor reset.
        if _use_keyframes:
            # Keyframe mode: only use start_image. end_image is removed because
            # Forge keyframes look nearly identical to each other (0.10 denoising),
            # so start+end being the same image forces LTX into a Ken Burns zoom.
            # Identity quality comes from the start_image alone.
            clip_start_image = _kf[i] if i < len(_kf) else prepped_photo
            clip_end_image   = None
            _is_chained = i > 0
        else:
            clip_start_image = _chain_frame if _chain_frame else prepped_photo
            clip_end_image   = None
            _is_chained = bool(_chain_frame)

        prompt_to_use = clip_prompt
        # Strip explicit camera direction words (zoom, pan, dolly) but do NOT
        # lock to "static shot" -- that suppresses all motion. The subject_anchor
        # and start_image handle identity; the prompt should drive movement.
        from features.fun_videos.multi_pipeline import _strip_camera_moves
        prompt_to_use = _strip_camera_moves(prompt_to_use)

        # Prepend subject anchor so every clip is grounded to the actual photo.
        if subject_anchor and not prompt_to_use.lower().startswith(subject_anchor[:20].lower()):
            prompt_to_use = subject_anchor + " " + prompt_to_use

        # Do NOT add "exact same location" prefix -- it suppresses all motion.
        # Identity is maintained by the Forge keyframe start_image and subject_anchor text.

        if not prompt_to_use.strip():
            prompt_to_use = subject_anchor or "Subject in atmospheric scene, natural movement, cinematic"
        finalized = _finalize_prompt(prompt_to_use, model_name, motion_style="narrative")
        if not finalized.strip():
            finalized = "Cinematic scene, natural movement, photorealistic, high quality"
        clip_out  = str(job_dir / f"clip_{i:02d}_{job.id[:6]}.mp4")
        _arc_dur  = _arc_entry.get("duration") if isinstance(_arc_entry, dict) else None
        this_dur  = float(_arc_dur) if _arc_dur else (clip_durations[i] if i < len(clip_durations) else clip_dur)
        this_dur  = max(4.0, min(12.0, this_dur))

        # Guidance 3.0: enough above the 2.8 near-static floor to produce visible
        # motion, but low enough that the conditioning start_image still dominates
        # identity. At 3.5 the model follows text so aggressively it transforms the
        # subject's appearance across clips.
        effective_guidance = min(guidance, 3.0)

        # Extract the audio segment for this clip's time window so LTX-2 can
        # condition the video generation directly on the music. WAV avoids MP3
        # Slices were pre-extracted before the loop -- just look up the path.
        _audio_slice: str | None = _audio_slices[i] if i < len(_audio_slices) else None

        try:
            clip_path = video_generator.generate_video(
                image_path=clip_start_image,
                prompt=finalized,
                out_path=clip_out,
                duration=this_dur,
                model_name=model_name,
                resolution=resolution,
                override_width=int(ow) if ow else None,
                override_height=int(oh) if oh else None,
                steps=steps,
                guidance=effective_guidance,
                seed=seed,
                end_image_path=clip_end_image,
                negative_prompt=video_generator.negative_prompt_for(model_name, motion_style="narrative"),
                audio_source=_audio_slice,
                stop_check=_stopped,
                log_fn=_log,
                progress_fn=_video_progress,
                worker_url=_worker_url or None,
            )
        except Exception as e:
            err = str(e)
            _log(f"[error] Clip {clip_num} failed: {err}")
            _last_error[0] = err
            if "out of memory" in err.lower() or "cuda error" in err.lower():
                import threading
                from services import manager as _svc
                threading.Thread(target=_svc.restart_service, args=("wangp",), daemon=True).start()
            break

        if not clip_path:
            # Timeout or copy failure -- restart WanGP to clear degraded state
            # and retry once. After ~20 clips WanGP can hang at Step 0 due to
            # VRAM fragmentation; a restart clears it in ~35 seconds.
            _log(f"[warning] Clip {clip_num} failed -- restarting WanGP and retrying once...")
            job.update(progress=pct_start, message=f"Clip {clip_num}/{n_clips} -- restarting WanGP, retrying...")
            import threading as _th
            from services import manager as _svc
            _svc.restart_service("wangp")
            # Wait for worker to come back (up to 90s)
            for _w in range(45):
                if _stopped():
                    break
                job.update(progress=pct_start, message=f"Clip {clip_num}/{n_clips} -- waiting for WanGP restart ({_w*2}s)...")
                time.sleep(2)
                try:
                    import urllib.request as _ur
                    with _ur.urlopen(f"http://127.0.0.1:7899/health", timeout=3) as _r:
                        if __import__("json").loads(_r.read()).get("ok"):
                            break
                except Exception:
                    pass
            if not _stopped():
                try:
                    clip_path = video_generator.generate_video(
                        image_path=clip_start_image,
                        prompt=finalized,
                        out_path=clip_out,
                        duration=this_dur,
                        model_name=model_name,
                        resolution=resolution,
                        override_width=int(ow) if ow else None,
                        override_height=int(oh) if oh else None,
                        steps=steps,
                        guidance=effective_guidance,
                        seed=seed,
                        end_image_path=clip_end_image,
                        negative_prompt=video_generator.negative_prompt_for(model_name, motion_style="narrative"),
                        audio_source=_audio_slice,
                        stop_check=_stopped,
                        log_fn=_log,
                        worker_url=_worker_url or None,
                        progress_fn=_video_progress,
                    )
                except Exception as _re:
                    clip_path = None
                    _log(f"[error] Clip {clip_num} retry also failed: {_re}")
            if not clip_path:
                _log(f"[error] Clip {clip_num} produced no output -- stopping early")
            break

        # Trim clip to exact beat-aligned duration so timing errors don't
        # accumulate across clips. WanGP may over/undershoot by up to ~0.5s.
        actual_dur = probe_duration(clip_path)
        if actual_dur and abs(actual_dur - this_dur) > 0.08:
            trimmed = clip_out.replace(".mp4", "_t.mp4")
            trim_r = subprocess.run(
                ["ffmpeg", "-y", "-i", clip_path, "-t", str(this_dur), "-c", "copy", trimmed],
                capture_output=True, timeout=60,
            )
            if trim_r.returncode == 0 and Path(trimmed).exists():
                os.replace(trimmed, clip_path)
                log.debug("[song-video] Clip %d trimmed %.2fs -> %.2fs", clip_num, actual_dur, this_dur)

        # -- Beat-sync via motion peak detection + speed ramp ----------------
        post_trim_dur = probe_duration(clip_path) or this_dur
        beat_pos      = beat_positions[i] if i < len(beat_positions) else 0.5
        target_time   = beat_pos * post_trim_dur
        ramped_out    = clip_out.replace(".mp4", "_synced.mp4")
        _beat_sync_useful = beat_pos > 0.02
        job.update(progress=pct_end, message=f"Clip {clip_num}/{n_clips} -- syncing peak to beat...")
        try:
            applied, info = align_clip_to_beat(
                clip_path, target_time, post_trim_dur, ramped_out,
            ) if _beat_sync_useful else (False, {"reason": "beat_pos near 0 or 0.5 -- skipped"})
            if applied and Path(ramped_out).exists():
                os.replace(ramped_out, clip_path)
                _log(
                    f"[info] Clip {clip_num} beat-synced: peak {info['natural_time']:.2f}s "
                    f"-> {info['target_time']:.2f}s (conf {info['confidence']:.2f})"
                )
            else:
                if os.path.exists(ramped_out):
                    try:
                        os.remove(ramped_out)
                    except OSError:
                        pass
                if info.get("reason"):
                    log.debug("[song-video] Clip %d sync skipped: %s", clip_num, info["reason"])
        except Exception as e:
            log.warning("[song-video] Beat-sync exception on clip %d: %s -- keeping original", clip_num, e)

        # Two-sided boundary trim for intermediate clips (not first, not last):
        # - Head trim (clips 1+): LTX-2 startup frames are brighter/different from
        #   the conditioning frame, causing a visible flash at transition. Trim 0.25s
        #   from the start so the xfade blends only stable frames.
        # - Tail trim (clips 0 to N-2): LTX-2 bakes a ~0.2s fade-out into every clip.
        #   Trim so the boundary frame is still in-motion.
        # Chain frame is extracted BEFORE these trims (below) so it always comes
        # from the raw generated output, not the trimmed version.
        _HEAD_TRIM = 0.08  # seconds to remove from start of clips 1+ (2 frames at 24fps)
        # Removing only 2 frames eliminates the LTX-2 startup flash while keeping
        # the frames closest to the conditioning image -- the best-anchored frames.
        _TAIL_TRIM = 0.20  # seconds to remove from end of clips 0 to N-2

        if i < n_clips - 1:
            clip_real_dur = probe_duration(clip_path) or this_dur
            trim_start = _HEAD_TRIM if i > 0 else 0.0
            trim_end   = max(0.0, clip_real_dur - _TAIL_TRIM)
            trim_dur   = max(trim_end - trim_start, clip_real_dur * 0.5)
            trim_out   = str(job_dir / f"clip_{i:02d}_fe.mp4")
            tr = subprocess.run(
                ["ffmpeg", "-y",
                 "-ss", f"{trim_start:.4f}", "-i", clip_path,
                 "-t", f"{trim_dur:.4f}", "-c", "copy", trim_out],
                capture_output=True, timeout=60,
            )
            if tr.returncode == 0 and Path(trim_out).exists():
                os.replace(trim_out, clip_path)
            else:
                log.debug("[song-video] Clip %d boundary trim failed -- using full clip", clip_num)
        elif i == n_clips - 1 and i > 0:
            # Last clip: head trim only (keep natural tail fade, remove startup flash).
            clip_real_dur = probe_duration(clip_path) or this_dur
            trim_out = str(job_dir / f"clip_{i:02d}_fe.mp4")
            tr = subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{_HEAD_TRIM:.4f}", "-i", clip_path,
                 "-c", "copy", trim_out],
                capture_output=True, timeout=60,
            )
            if tr.returncode == 0 and Path(trim_out).exists():
                os.replace(trim_out, clip_path)

        # Per-clip upscale when lip sync forced 360p generation.
        # Upscaling each clip individually (before concat) is cleaner than
        # upscaling the final merged video, which can blur across boundaries.
        if lip_sync_res_active and th <= 360:
            up_out = clip_path.replace(".mp4", "_up.mp4")
            try:
                from core.upscaler import upscale_video
                up_result, up_err = upscale_video(clip_path, up_out, scale=1032/640, method="ffmpeg")
                if up_result and Path(up_result).exists():
                    os.replace(up_result, clip_path)
                    log.debug("[song-video] Clip %d upscaled 360p->580p", clip_num)
                else:
                    log.debug("[song-video] Clip %d upscale failed (%s) -- keeping 360p", clip_num, up_err)
            except Exception as _ue:
                log.debug("[song-video] Clip %d upscale exception: %s", clip_num, _ue)

        clip_paths.append(clip_path)
        _clip_secs.append(time.time() - _clip_t0)

        job.meta.update({
            "clips_done":  len(clip_paths),
            "clips_total": n_clips,
            "stage":       "generating",
        })

        # Extract chain frame AFTER all processing (beat-sync + tail-trim) so
        # the frame given to the next clip matches what this clip actually ends on.
        # No source-photo blending: blending compounds across 30+ clips
        # (0.85^30 leaves only 1% of the original motion state), converging every
        # chain frame toward the source photo -- exactly the "same image" symptom.
        # Identity is maintained via subject_anchor text in every prompt instead.
        if i < n_clips - 1:
            _cframe_path = str(job_dir / f"chain_{i:02d}.png")
            _chain_frame = _extract_last_frame(clip_path, _cframe_path)
            if not _chain_frame:
                log.info("[song-video] Frame extraction failed for clip %d -- next clip uses source", clip_num)
            else:
                log.info("[song-video] Clip %d chain frame: %s", clip_num, _cframe_path)

            if reanchor_every > 0 and (i + 1) % reanchor_every == 0:
                log.info("[song-video] Re-anchoring clip %d to source photo (every %d)", i + 2, reanchor_every)
                _chain_frame = None

        if _clip_secs and clip_num < n_clips:
            avg = sum(_clip_secs) / len(_clip_secs)
            rem = (n_clips - clip_num) * avg
            _log(f"[info] Clip {clip_num}/{n_clips} complete -- ~{int(rem // 60)}m {int(rem % 60):02d}s remaining")
        else:
            _log(f"[info] Clip {clip_num}/{n_clips} complete")

    if _stopped():
        return

    if not clip_paths:
        raw = _last_error[0] or "No clips generated -- check WanGP is running"
        raise RuntimeError(f"Song video failed: {raw}")

    job.meta["stage"] = "concatenating"
    job.update(progress=79, message=f"Concatenating {len(clip_paths)} clips...")
    job.meta["clips_generated"] = len(clip_paths)

    # -- Phase 2: Concat with xfade transitions
    # Collect per-clip durations for correct xfade offset math.
    clip_durations = [probe_duration(p) or 4.0 for p in clip_paths]
    concat_path = str(job_dir / f"concat_{job.id[:6]}.mp4")
    _xfade_dur = settings.get("_song_xfade_dur", 0.75)
    if not _concat_with_xfade(clip_paths, clip_durations, concat_path, fade_dur=_xfade_dur):
        log.warning("[song-video] xfade concat failed -- falling back to hard cut")
        if not _concat_clips(clip_paths, concat_path):
            concat_path = clip_paths[0]

    effective_dur = audio_dur if audio_dur > 0 else (probe_duration(audio_path) or 0.0)
    if effective_dur <= 0:
        raise RuntimeError("Cannot determine audio duration -- file may be missing or corrupt")

    # Beat-sync DTW pass removed: clips are already beat-aligned by the pipeline
    # (clip durations snap to beat boundaries during analysis). Running DTW again
    # adds 2-3 min of pure overhead per video with no quality benefit.
    video_to_loop = concat_path

    # -- Phase 3: Loop to fill song + merge audio -----------------------------
    job.meta["stage"] = "merging"
    job.update(progress=92, message="Looping clips to fill song duration...")

    model_tag  = model_name.split()[0].lower()
    final_path = str(job_dir / f"songvid_{model_tag}_{time.strftime('%H%M%S')}.mp4")
    merged     = _merge_video_audio_trim(video_to_loop, audio_path, final_path, effective_dur, pad_before=pad_before)

    if merged:
        # Upscale only when an explicit low-res override was requested (e.g. fast mode).
        # Native 580p output does not need upscaling.
        if ow and oh and int(oh) <= 360 and not _stopped():
            job.update(progress=97, message="Upscaling to 720P...")
            try:
                from core.upscaler import upscale_video
                up_path = merged.replace(".mp4", "_720p.mp4")
                up_out, up_err = upscale_video(merged, up_path, scale=2.0, method="ffmpeg")
                if up_out and Path(up_out).exists():
                    merged = up_out
                    log.info("[song-video] Upscaled 360p -> 720p: %s", Path(up_out).name)
                else:
                    log.warning("[song-video] Upscale failed (%s) -- keeping 360p output", up_err)
            except Exception as _ue:
                log.warning("[song-video] Upscale exception: %s -- keeping 360p", _ue)

        job.output = merged
        from core.inbox import copy_to_inbox; copy_to_inbox(job.output)
        job.meta.update({"final_path": merged, "audio_path": audio_path})
        job.message = f"Music video complete! ({len(clip_paths)} clips)"

        # Auto-evaluate quality so regressions surface in the log without manual review.
        try:
            from features.song_video.evaluator import evaluate_video
            from app import get_llm_router
            eval_result = evaluate_video(
                merged, len(clip_paths), clip_dur,
                xfade_dur=settings.get("_song_xfade_dur", 0.12),
                llm_router=get_llm_router(),
            )
            score = eval_result.get("score", -1)
            avg_diff = eval_result.get("avg_diff", -1)
            vision = eval_result.get("vision_report") or ""
            issues = eval_result.get("issues", [])
            log.info("[eval] Quality score: %.1f/10 | avg seam diff: %.1fpx | %s",
                     score, avg_diff, "; ".join(issues) if issues else "no issues detected")
            if vision:
                log.info("[eval] Vision: %s", vision)
            job.meta["eval"] = {"score": score, "avg_diff": avg_diff,
                                "issues": issues, "vision": vision}
        except Exception as _ev:
            log.debug("[eval] Auto-evaluation failed (non-fatal): %s", _ev)

        try:
            norm = merged.replace("\\", "/")
            idx  = norm.lower().find("/output/")
            url  = norm[idx:] if idx != -1 else f"/output/{Path(merged).name}"
            gallery_push(
                url, tab="music-video",
                prompt=(story_arc[0].get("prompt", "") if isinstance(story_arc[0], dict) else str(story_arc[0]))[:120] if story_arc else "",
                model=model_name,
                metadata={
                    "path": merged,
                    "job_id": job.id,
                    "clips": len(clip_paths),
                },
            )
        except Exception as e:
            log.warning("gallery_push failed: %s", e)

        try:
            from core.session import get_current as get_session
            get_session().add_file(Path(merged).name, "video", "song_video", path=merged)
        except Exception as e:
            log.warning("session.add_file failed: %s", e)

        # Clean up intermediate clip files and concat
        for cp in clip_paths:
            try:
                os.remove(cp)
            except Exception:
                pass
        if concat_path and concat_path != merged:
            try:
                os.remove(concat_path)
            except Exception:
                pass
    else:
        # Merge failed -- log it but do NOT inbox a soundless video.
        # The clip files remain in job_dir for manual recovery if needed.
        log.error("[song-video] Audio merge failed for job %s -- no output produced", job.id)
        job.message = f"Audio merge failed ({len(clip_paths)} clips generated but not merged)"
        raise RuntimeError(
            f"Audio merge failed -- {len(clip_paths)} clips were generated but could not be "
            f"merged with audio. Clip files are in {job_dir}"
        )


def _cleanup_gpu_phase_temps(job_dir: Path, audio_wav: str | None) -> None:
    """Remove per-job temp files that are no longer needed after GPU phase completes."""
    import shutil as _shutil
    # Audio slices (per-clip WAVs for lip sync conditioning)
    slices_dir = job_dir / "audio_slices"
    if slices_dir.exists():
        try:
            _shutil.rmtree(slices_dir)
        except Exception:
            pass
    # Chain frame PNGs (used only to link consecutive clips)
    for png in job_dir.glob("chain_*.png"):
        try:
            png.unlink()
        except Exception:
            pass
    # Per-job WAV in temp dir
    if audio_wav and os.path.isfile(audio_wav):
        try:
            os.remove(audio_wav)
        except Exception:
            pass
