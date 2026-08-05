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
import random
import shutil
import subprocess
import time
from pathlib import Path

from core.ffmpeg_utils import probe_duration
from core.llm_client import TIER_BALANCED, TIER_FAST, encode_image_b64, parse_json_response
from features.fun_videos.pipeline import _prep_photo, _finalize_prompt
from features.fun_videos.multi_pipeline import _concat_clips, _concat_with_xfade

log = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"


# Largest REQUESTED frame proven to render on a 16GB card with LTX-2 Dev19B:
# the DCMVS lip-sync size. Bigger requests round up past what the card carries
# and hang at step 0 rather than erroring, so this is a hard ceiling, not a
# preference. Raise it only with a rendered clip as evidence.
SAFE_REQ_PIXELS = 960 * 544
SAFE_FALLBACK_RES = (960, 544)
ROOMY_VRAM_GB = 24.0


def _detected_vram_gb() -> float | None:
    """Total VRAM in GB, or None if it cannot be measured."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return round(int(r.stdout.strip().splitlines()[0].strip()) / 1024, 1)
    except Exception:
        pass
    return None


def _clamp_res_for_gpu(w: int, h: int, log) -> tuple:
    """Hold the requested frame to what this GPU can actually finish.

    Fails CLOSED when VRAM is unmeasurable: a smaller render is a recoverable
    disappointment, a step-0 deadlock hangs the worker until the poll timeout
    and looks to the user like the app is simply broken.
    """
    if w * h <= SAFE_REQ_PIXELS:
        return w, h
    vram = _detected_vram_gb()
    if vram and vram >= ROOMY_VRAM_GB:
        return w, h
    sw, sh = SAFE_FALLBACK_RES
    log.warning(
        "[song-video] resolution clamp: %dx%d -> %dx%d (VRAM %s GB). LTX-2 rounds "
        "the frame up to a 64-multiple and anything above %dx%d deadlocks at step 0 "
        "on this card instead of erroring.",
        w, h, sw, sh, ("%.1f" % vram) if vram else "unknown", *SAFE_FALLBACK_RES)
    return sw, sh


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

def _extract_subject_anchor(photo_path: str, llm_router) -> tuple:
    """Vision call to get a 12-15 word subject description prepended to every prompt.

    Returns (anchor, scenery): scenery=True means the image has no face-bearing
    subject, so nothing can gesture or lip-sync -- callers should warn loudly.
    Runs concurrently with beat analysis in run_song_prep so it adds ~0 wall time.
    Same pattern as multi_pipeline._generate_subject_anchor.
    """
    if not photo_path or not os.path.isfile(photo_path):
        return "", False
    try:
        b64 = encode_image_b64(photo_path)
        if not b64:
            return "", False
        raw = llm_router.route_vision(
            "Describe the main subject's APPEARANCE in 15-20 words. "
            "The subject may be human, animal, creature, object, or fantasy -- describe whatever is there. "
            "Focus on: color, texture, material, shape, distinguishing features. "
            "Output ONLY the description, no preamble like 'The subject is' or 'I see'. "
            "If the image is pure scenery/landscape/architecture with NO character, creature, "
            "person, animal, or figure that has a face, begin your answer with exactly 'SCENERY: '. "
            "Examples: "
            "'Dusty rose skull, exposed cheekbones, dark hollow eye sockets, root tendrils at jaw.' "
            "'Brown teddy bear, worn felt nose, dark bead eyes, soft fluffy ears.' "
            "'Red-haired woman, blue denim jacket, pale freckled skin, hazel eyes.' "
            "'SCENERY: Moss-covered stone archway, weathered brick, green vegetation.'",
            [b64], tier=TIER_FAST, max_tokens=90,
        )
        import re as _re
        # Detect the no-face tag on the RAW reply, before any cleanup can eat it,
        # and tolerate sloppy variants (markdown wrapper, missing colon, lowercase).
        scenery = bool(_re.match(r'^[\s#*>"\'`-]*scenery\b', raw.strip(), _re.IGNORECASE))
        anchor = raw.strip()
        anchor = _re.sub(r'^#+\s*[^\n]*\n+', '', anchor)
        anchor = _re.sub(r'\*\*([^*]+)\*\*', r'\1', anchor)
        anchor = _re.sub(r'\*([^*]+)\*', r'\1', anchor)
        anchor = anchor.strip().strip('"').strip("'").split('\n')[0].strip()
        anchor = _re.sub(r"^scenery\b[\s:,.-]*", "", anchor, flags=_re.IGNORECASE).strip()
        # Unhelpful non-description responses ("there is no person...") also mean
        # no usable subject was found -- treat them as the scenery case.
        bad_starts = ("i don't see", "i cannot", "i can't", "there is no", "no person",
                      "i see no", "i'm unable", "i am unable", "the image shows no")
        if any(anchor.lower().startswith(b) for b in bad_starts):
            log.warning("[song-video] Subject anchor rejected unhelpful response: %r", anchor[:80])
            return "", True
        if anchor and not anchor.endswith("."):
            anchor += "."
        return anchor, scenery
    except Exception as e:
        log.warning("[song-video] Subject anchor extraction failed (non-fatal): %s", e)
        return "", False


# -- Story arc generation ------------------------------------------------------

def _varied_fallback_arc(user_idea: str, n_clips: int) -> list:
    """No-LLM story arc: cycle distinct motions so clips NEVER share one prompt.

    Handing every clip the same prompt renders as one static shot drifting for
    the whole song -- every path that can't get real arc prompts must come
    through here, loudly, instead of duplicating a single base prompt.
    """
    base = (user_idea or "Subject in original scene").strip()
    motions = [
        "head turns slowly to one side, then returns to center",
        "one arm rises partway, hand opening, then lowers",
        "torso leans forward, holds, then straightens back up",
        "weight shifts to one hip, gentle sway, then settles",
        "chin lifts, eyes close briefly, then head levels again",
        "one shoulder rolls back, chest expands, then relaxes",
        "hand reaches forward, fingers spreading, then retracts",
        "body rocks gently side to side, hair and clothing stirring",
    ]
    return [{"prompt": f"{base} -- {motions[i % len(motions)]}", "duration": 7.0}
            for i in range(n_clips)]


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

FRAMING LOCK (mandatory every prompt):
The subject's face stays fully inside the frame for the whole clip.
Never crop the head, never let the subject leave frame -- the face carries
the lip-sync, so a cropped or exiting face ruins the entire clip.

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
) -> list[str]:
    """Generate N motion prompts that follow a single story across the song.

    Passes per-clip lyrics so the LLM can align the action to the words.
    """
    clip_labels = analysis.get("clip_energy_labels", [])
    bpm    = analysis.get("bpm")
    key    = analysis.get("key", "")
    mode   = analysis.get("mode", "")
    mood   = analysis.get("mood", "")
    clip_durations  = clip_durations or []

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
        lyrics_snip = _clip_lyrics(i)
        lyric_part = f" | lyrics: \"{lyrics_snip}\"" if lyrics_snip else ""
        clip_hints.append(
            f"Clip {i + 1:02d} ({dur:.0f}s | {label} energy){lyric_part}"
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
            if result:
                log.info("[song-video] Story arc: %d prompts from LLM, padded to %d", src, n_clips)
                return result
            # Valid JSON whose entries all lacked a "prompt" key: NOT a success --
            # fall through to the varied fallback instead of returning [] silently.
            log.warning("[song-video] Story arc JSON parsed but had no usable 'prompt' entries")
    except Exception as e:
        log.warning("[song-video] Story arc LLM call failed: %s", e)

    log.warning("[song-video] Story arc unavailable -- using local varied-motion fallback for %d clips", n_clips)
    return _varied_fallback_arc(user_idea, n_clips)


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

    gap = target_dur - video_dur
    # Three fill modes, in preference order:
    #   none   -- video already covers the song: trim the surplus down (invisible).
    #   freeze -- video is modestly short: HOLD the last frame to fill the gap.
    #             A held final frame reads as an intentional ending shot; the old
    #             behaviour (-stream_loop, restart from clip 1) put a second, un-
    #             synced copy of the mouth over the song's back half. With the
    #             route now over-covering the song, the gap should be small.
    #   loop   -- video is drastically short (< ~half the song): something upstream
    #             under-generated. A multi-second freeze would look broken, so fall
    #             back to looping and flag it loudly.
    if video_dur <= 0 or gap <= 0.05:
        fill = "none"
    elif gap <= max(2.0, video_dur * 0.5):
        fill = "freeze"
    else:
        fill = "loop"
        log.warning("[song-video] merge: video %.2fs is drastically short of target %.2fs "
                    "(gap %.2fs) -- upstream under-generated; looping as a last resort",
                    video_dur, target_dur, gap)
    log.info("[song-video] merge: video=%.2fs song=%.2fs target=%.2fs gap=%.2fs fill=%s "
             "(full song always plays; audio is never trimmed below the song)",
             video_dur, true_audio_dur, target_dur, gap, fill)

    # Build audio filter: delay by pad_before ms if requested. apad pads with
    # silence AFTER the song so the output can reach target_dur without ever
    # truncating the song itself.
    audio_filter = f"adelay={int(pad_before * 1000)}|{int(pad_before * 1000)},apad" if pad_before > 0 else "apad"

    if fill == "none":
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
    elif fill == "freeze":
        # tpad clones the last frame for the gap (+0.5s slack; -t trims exact).
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-filter_complex",
            f"[0:v]tpad=stop_mode=clone:stop_duration={gap + 0.5:.3f}[v];[1:a]{audio_filter}[a]",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "15",
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{target_dur:.3f}",
            "-movflags", "+faststart",
            out_path,
        ]
    else:  # loop
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

    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600)
        if r.returncode == 0 and Path(out_path).exists():
            # Safety net: the output must contain the whole song. If it ever
            # comes up short (some odd codec/keyframe edge case), flag it loudly
            # rather than silently shipping a clipped song.
            out_dur = probe_duration(out_path) or 0.0
            if out_dur + 0.2 < true_audio_dur:
                log.error("[song-video] OUTPUT IS SHORT: %.2fs < song %.2fs -- the song would be "
                          "clipped. Investigate the merge.", out_dur, true_audio_dur)
            else:
                log.info("[song-video] merge ok: output %.2fs (song %.2fs preserved)", out_dur, true_audio_dur)
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

    # Fire Anthropic vision call in background (pure I/O, no CPU competition).
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as api_pool:
        fut_anchor = api_pool.submit(_extract_subject_anchor, photo_path, llm_router) if photo_path else None

        # CPU tasks run sequentially -- each gets full cores.
        clip_durations, _ = compute_clip_plan(
            audio_path, n_clips, clip_dur, max(float(clip_dur), 10.0)
        )
        if not lyrics_text and os.path.isfile(audio_path):
            detected = _transcribe_lyrics(audio_path)
            if detected:
                lyrics_text = detected
                log.info("[song-video] Auto-detected %d chars of lyrics", len(lyrics_text))

        subject_anchor, anchor_scenery = fut_anchor.result() if fut_anchor else ("", False)
    if anchor_scenery:
        warn = ("Source image has no face or figure -- nothing can gesture or lip-sync, so the "
                "clips will drift like a slow pan. Use a creature/character with a visible face.")
        log.warning("[song-video] SUBJECT WARNING: %s (anchor: %s)", warn, subject_anchor[:60] or "none")
        job.meta["subject_warning"] = warn
    if subject_anchor:
        log.info("[song-video] Subject anchor: %s", subject_anchor[:80])

    # Light guard only: the planner already fits durations to the song (each in
    # [clip_dur, max(clip_dur,10)]) and the feasibility clamp + boundary guard
    # prevent degenerate clips. Clamp to the same [4,12] band the GPU phase uses
    # for this_dur -- this avoids ffmpeg -t 0 without INFLATING the plan (the old
    # max(8, min(10, d)) forced every clip to >=8s, blowing a 32s song up to 48s).
    clip_durations = [max(4.0, min(12.0, d)) for d in clip_durations]
    settings["_clip_durations"] = clip_durations
    log.info("[song-video] Clip durations: %s", clip_durations)

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
                                  clip_durations=clip_durations)
        settings["_story_arc"] = arc
        log.info("[song-video] Story arc (%d clips) generated", n_clips)
    except Exception as e:
        log.warning("[song-video] Story arc failed: %s", e)
        arc = [user_idea or "Subject erupts into motion"] * n_clips
        settings["_story_arc"] = arc

    # TEMPORARILY RE-ENABLED 2026-08-01 per Andrew's explicit request after the
    # original "never reanchor" choice let a 5-clip chain drift completely off
    # the source subject by clip 4 (crocheted mushroom -> unrelated furry
    # creature, no stitching, background gone). The devs' tradeoff (continuity
    # over fidelity, since a reanchor "visibly repeats the same opening shot")
    # is real, but for THIS use case -- an unmistakable, specific physical
    # object the video must stay recognizable as -- losing the subject entirely
    # is worse than an occasional visible snap-back. Reanchor every 2 clips
    # (not every clip) to still get some continuity between adjacent pairs.
    reanchor_every = 2
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
        log.warning("[song-video] No story arc reached the render phase -- using varied-motion fallback")
        story_arc = _varied_fallback_arc(settings.get("video_prompt", ""), n_clips)
    if not audio_path or not os.path.isfile(audio_path):
        raise RuntimeError("Audio file not found -- please re-upload the song")

    ts      = time.strftime("%Y-%m-%d")
    slug    = Path(photo_path).stem[:14].replace(" ", "_") if photo_path else "songvid"
    job_dir = OUTPUT_DIR / ts / f"songvid_{slug}_{job.id}"
    job_dir.mkdir(parents=True, exist_ok=True)

    # Resolution strategy:
    # - Lip sync ON + audio WAV available: generate natively at 960x544 -- the
    #   DCMVS-proven lip-sync recipe (2026-07-29: cpuguard/sync root-caused the
    #   mouth-box artifact -- audio tokens do NOT overflow at 960x544, only above
    #   ~580p/1032x580; the old 640x360-then-upscale path was a workaround for a
    #   threshold that was never actually being approached here, and the upscale
    #   pass itself softened detail the native resolution doesn't need).
    # - Lip sync OFF or no audio WAV: native 580p, audio conditioning unavailable.
    # - Explicit override: honour ow/oh directly.
    # Forge keyframe dependency removed: chain-frame anchoring alone is sufficient
    # to prevent identity drift. Forge is not required.
    # 2026-08-01: 960x544 confirmed HUNG on this card -- GPU sat at 9% util /
    # 2.4GB VRAM during the stall, so it isn't a VRAM-fit problem the
    # model_vram_error() gate could ever catch (that gate only checks the
    # named model's registered floor -- it has no visibility into overrides
    # or into lip_sync silently swapping the render resolution out from under
    # it). Blocking both paths to 960x544 outright until someone actually
    # roots out why generation stalls at that resolution, rather than
    # widening the VRAM gate to "fix" a bug that was never about VRAM.
    _lip_sync_res_active = False
    # 2026-08-03: override re-allowed for lip_sync jobs ONLY (manager-approved
    # A/B). The 08-01 hang that justified force-native was diagnosed inside the
    # same GPU-TDR/bad-driver window as the native-conditioning "deadlock" that
    # did NOT reproduce today on the reinstalled driver (jobs 7b73161046fc,
    # a2e6f1b8510d rendered clean). Conditioning at 1032x580 applies but does
    # not grip the mouth (frame/RMS anti-correlation, 21:25 board post); the
    # July recipe that produced AWM00001 rendered 640x360 then upscaled.
    # Non-lip_sync jobs keep the 83881b3 force-native behavior unchanged.
    if ow and oh and bool(settings.get("lip_sync", False)):
        tw, th = int(ow), int(oh)
        log.info("[song-video] lip_sync override honored: rendering %dx%d "
                 "(explicit output_width/height on a lip_sync job)", tw, th)
    elif bool(settings.get("lip_sync", False)):
        # Tier-1 DCMVS port (2026-08-04, verify-after-driver-rollback): lip-sync
        # jobs DEFAULT to DCMVS's proven 960x544 (worker/VAE emits 960x512).
        # Sits below the audio-token overflow threshold (580p+ silently degrades
        # conditioning grip) and well above the old 640x360 workaround in detail;
        # proven by the 07-29 local A/B and tonight's clean-env pod render.
        # Explicit overrides above still win; non-lip_sync jobs unchanged below.
        tw, th = 960, 544
        log.info("[song-video] lip_sync default resolution: 960x544 (DCMVS proven)")
    else:
        if ow and oh:
            log.warning("[song-video] Ignoring override_width/override_height "
                        "(%sx%s requested) -- non-model resolutions are hanging "
                        "on this GPU as of 2026-08-01, forcing model default.", ow, oh)
        _native = video_generator.MODELS.get(model_name, {}).get("res") or (1032, 580)
        tw, th = _native
    # 2026-08-04 (Andrew's own studio test, job 92de7ecf7fb4 21:09): the
    # NON-lip_sync path took the registered native 1032x580 and deadlocked --
    # "Step 0/8" repeating every 2s forever, GPU pinned, no progress, no error.
    # LTX-2 rounds the frame UP to a 64-multiple (1032x580 -> 1088x640, 33pct
    # more pixels than the proven 960x544 -> 960x512) and a 16GB card cannot
    # carry it. The lip_sync branch above got a proven size months ago; this
    # branch never did, so every plain song-video job on this box was dead on
    # arrival. Clamp applies to ALL paths -- an explicit override cannot opt
    # into a deadlock either.
    tw, th = _clamp_res_for_gpu(tw, th, log)
    # lane 3C: on the lip_sync path, clamp video guidance down to the model's
    # registered value (3.0 for Distilled) -- documented: >3.5 makes text
    # guidance fight the source identity, and no layer below here clamps it
    # (video_generator.py:439 passes guidance_scale straight through). Same
    # lip_sync-only scoping as the resolution override above.
    if bool(settings.get("lip_sync", False)):
        _reg_g = video_generator.MODELS.get(model_name, {}).get("guidance")
        if _reg_g and float(guidance) > float(_reg_g):
            log.info("[song-video] lip_sync guidance clamp: %s -> %s (model registered)",
                     guidance, _reg_g)
            guidance = float(_reg_g)
    log.info("[song-video] effective render params: %dx%d model=%s steps=%s "
             "guidance=%s lip_sync=%s auto_lipsync=%s best_of_n=%s",
             tw, th, model_name, steps, guidance,
             bool(settings.get("lip_sync", False)),
             bool(settings.get("auto_lipsync", False)),
             settings.get("best_of_n", 1))

    if photo_path and os.path.isfile(photo_path):
        shutil.copy2(photo_path, job_dir / f"source{Path(photo_path).suffix}")

    # -- GPU: acquire WanGP exclusively (orchestrator evicts everything else)
    from core.gpu_orchestrator import gpu
    gpu.acquire("wangp", reason=f"song-video {n_clips} clips")
    try:
        _do_song_gpu_phase(
            job, photo_path, settings, job_dir,
            n_clips, clip_durations, model_name,
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


def _seed_pool(base_seed, n, clip_index):
    """Distinct, reproducible seeds for the N best-of-N attempts of one clip.
    A real requested seed (>=0) goes first; the rest come from a per-clip
    deterministic RNG so re-runs reproduce but attempts within a clip differ."""
    rng = random.Random(10_000 + int(clip_index))
    seeds: list[int] = []
    if base_seed is not None and int(base_seed) >= 0:
        seeds.append(int(base_seed))
    while len(seeds) < n:
        s = rng.randint(1, 2**31 - 1)
        if s not in seeds:
            seeds.append(s)
    return seeds[:max(1, n)]


class SyncFloorNotMet(RuntimeError):
    """A VOICED window produced no take that actually lip-syncs.

    Deliberately NOT a None return: the caller reads None as a dead renderer and
    answers with a WanGP restart plus an unscreened re-render. This says
    something different -- the renderer worked fine, and every take it produced
    has a dead mouth over singing.
    """


# Acceptance floor for a window that carries real vocal energy. 2026-08-04:
# shipping "best available" on such windows is exactly how 79 seconds of
# undriven mouth reached delivery in every cut of the night -- each clip was the
# best of its batch, and every one of them was a statue over a sung verse.
SYNC_RANK_FLOOR = 0.12


def _pick_best_seed(clip_index, out_path, gen_fn, audio_slice, base_seed, n, log_fn=print,
                    require_sync=False, min_rank=SYNC_RANK_FLOOR):
    """Generate up to `n` seeds for one clip, screen them, rank the survivors by
    audio<->mouth sync, and keep the best.

    require_sync=True is SYNC-OR-DIE, and it is set for windows measured to carry
    vocal energy. Such a window may only bank a take that is verdict=synced AND
    ranks >= min_rank; if none of the `n` takes clears that bar it raises
    SyncFloorNotMet rather than banking a statue. require_sync=False (a window
    with no real singing in it) keeps the older behaviour, where a clean but
    static take is the CORRECT content -- a resting mouth through an
    instrumental bar is right, and burning takes chasing a "synced" verdict on
    silence is waste (measured: window 01_0, 0 synced in 6 takes, nothing to
    sync to).

    Returns the chosen clip path (== out_path), or None if every take failed to
    produce a file at all."""
    from features.song_video import sync_qc
    seeds = _seed_pool(base_seed, n, clip_index)
    base, ext = os.path.splitext(out_path)
    best = None            # (score, path) -- artifact-clean takes only
    worst_fallback = None  # (score, path) -- screened-out takes, last resort
    for sd in seeds:
        attempt = f"{base}_s{sd}{ext}"
        res_path = gen_fn(sd, attempt)
        if not res_path or not os.path.isfile(res_path):
            log_fn(f"[best-of-{len(seeds)}] seed {sd}: no output -- skipped")
            continue
        # ARTIFACT SCREEN BEFORE RANKING (LIPSYNC_LEDGER: gates run before the
        # rank, not after). Sync score and slop RISE TOGETHER -- the takes that
        # move the mouth most are also the likeliest to ribbon -- so ranking
        # first and screening later systematically selects the dirtiest take
        # that scored well. Only the RIBBON verdict rejects here: it is the one
        # metric validated against both a known-good and a known-bad. Red
        # strands and dark carved text are surfaced for the eye elsewhere and
        # must never auto-reject (an eye-clean take once out-scored the
        # known-bad reference on red).
        _ribbon = None
        try:
            from features.song_video.artifact_screens import screen_window
            _sc = screen_window(res_path)
            _ribbon = _sc["ribbon_verdict"]
            # DECODE FAILURE IS NOT AN ARTIFACT VERDICT. An empty series scores
            # p95=999 and reads as "infested", so a missing/zero-byte/truncated
            # /audio-only file was being reported to the operator as "ribbon
            # artifacts (p95 999.00)" -- a confidently wrong diagnosis of a
            # file ffprobe simply could not read. Say what actually happened and
            # let ranking decide, matching how an exception in the screen behaves.
            if _sc["ribbon"]["n"] == 0:
                log_fn(f"[best-of-{len(seeds)}] seed {sd}: artifact screen could not "
                       f"decode this take (0 frame-pairs) -- not an artifact verdict")
                _ribbon = None
            elif _ribbon == "infested":
                # HELD BACK, not discarded. Returning None when every take is
                # infested makes the caller read the clip as a DEAD RENDER: it
                # restarts the WanGP worker, waits up to 90s, and re-renders
                # once with no screen at all -- so the screen throws away N real
                # takes and then ships an unscreened one. Measured on this
                # session's own output, 41% of takes are infested, so at
                # best_of_n=3 roughly 7% of clips would take that path and a
                # 12-clip job would hit it more often than not. Keep the take as
                # a last resort instead; a screened-and-flagged clip beats a
                # pointless service restart followed by an unscreened one.
                _p95 = _sc["ribbon"]["p95"]
                log_fn(f"[best-of-{len(seeds)}] seed {sd}: ribbon artifacts "
                       f"(p95 {_p95:.2f}) -- held back")
                # Ranked by ribbon score, not sync score: this take has not been
                # scored yet, and among takes we would rather not ship, the
                # least-infested one is the right last resort.
                if worst_fallback is None or _p95 < worst_fallback[0]:
                    worst_fallback = (_p95, res_path)
                continue
            if _ribbon == "eye-check":
                log_fn(f"[best-of-{len(seeds)}] seed {sd}: ribbon p95 "
                       f"{_sc['ribbon']['p95']:.2f} is in the eye-check band -- "
                       f"kept, but look at this one before shipping")
        except Exception as _e:
            # A screen that cannot run must not silently pass takes: say so, and
            # let ranking proceed rather than losing the clip entirely.
            log_fn(f"[best-of-{len(seeds)}] seed {sd}: artifact screen unavailable ({_e})")
        try:
            r = sync_qc.analyze(res_path, audio_path=audio_slice)
            score = sync_qc.mouth_sync_score(r)
            log_fn(f"[best-of-{len(seeds)}] seed {sd}: score={score:.3f} "
                   f"sync_y={r.get('sync_y')} contrast={r.get('sync_contrast')} "
                   f"motion={r.get('total_motion')}")
        except Exception as _e:
            r, score = None, 0.0
            log_fn(f"[best-of-{len(seeds)}] seed {sd}: QC failed ({_e}) -- scored 0")
        _synced = bool(r is not None and sync_qc.is_synced(r))
        _clears_floor = _synced and score >= min_rank
        if _clears_floor:
            # On a voiced window this is the ONLY thing that may be banked, so
            # rank only among takes that actually sync.
            if best is None or score > best[0]:
                best = (score, res_path)
        elif not require_sync:
            # Unvoiced window: a clean static take is correct content here.
            if best is None or score > best[0]:
                best = (score, res_path)
        else:
            log_fn(f"[best-of-{len(seeds)}] seed {sd}: below the sync floor "
                   f"(synced={_synced} rank={score:.3f} < {min_rank}) -- not bankable "
                   f"on a window that carries singing")
        # Early-accept a take that clears the bar AND is not known-dirty.
        # `_ribbon is None` means the screen could not run or could not decode --
        # treated as NO INFORMATION, not as a failure, because requiring
        # == "clean" made early-accept structurally impossible whenever the
        # screen was unavailable (silently burning all N seeds). Only the
        # eye-check band deliberately keeps looking for a cleaner take.
        _good_enough = _clears_floor if require_sync else _synced
        if _good_enough and _ribbon in (None, "clean"):
            log_fn(f"[best-of-{len(seeds)}] seed {sd} cleared the sync gate"
                   f"{f' (rank {score:.3f})' if require_sync else ''}"
                   f"{' and is artifact-clean' if _ribbon == 'clean' else ''} -- keeping it")
            break
    if best is None and require_sync:
        # SYNC-OR-DIE. This window carries real singing and not one of the
        # takes drove the mouth. Banking "the best available" here is exactly
        # how 79 seconds of statue-over-a-sung-verse reached delivery in every
        # cut of 2026-08-04: each clip WAS the best of its batch. Raise a
        # DISTINCT error rather than returning None, so the caller does not
        # mistake this for a dead renderer and answer with a pointless worker
        # restart plus an unscreened re-render.
        raise SyncFloorNotMet(
            f"clip {clip_index}: none of {len(seeds)} takes lip-synced this VOICED "
            f"window (needed verdict=synced and rank >= {min_rank}). Refusing to "
            f"bank a still mouth over singing. Raise best_of_n, check the "
            f"conditioning slice actually contains the vocal, or mark this window "
            f"as instrumental if it genuinely is.")
    if best is None and worst_fallback is not None:
        # Every take was screened out on an UNVOICED window. Ship the least-dirty
        # one rather than returning None: the caller treats None as a dead render
        # and answers with a WanGP restart plus an UNSCREENED retry, so returning
        # None here both wastes the renders we already paid for and defeats the
        # screen. (On a voiced window the sync floor above has already fired.)
        _p95, _fb = worst_fallback
        log_fn(f"[best-of-{len(seeds)}] every take showed ribbon artifacts -- "
               f"shipping the least-affected (p95 {_p95:.2f}). LOOK AT THIS CLIP: "
               f"the screen wanted to reject all {len(seeds)} of them.")
        best = (0.0, _fb)
    if best is None:
        return None
    score, best_path = best
    if os.path.abspath(best_path) != os.path.abspath(out_path):
        try:
            shutil.copy2(best_path, out_path)
        except Exception:
            out_path = best_path  # fall back to using the winner in place
    # Remove the non-winning attempt files to control disk.
    for sd in seeds:
        attempt = f"{base}_s{sd}{ext}"
        if os.path.isfile(attempt) and os.path.abspath(attempt) != os.path.abspath(out_path):
            try:
                os.remove(attempt)
            except Exception:
                pass
    log_fn(f"[best-of-{len(seeds)}] kept score={score:.3f}")
    return out_path


class GuideIsolationError(RuntimeError):
    """Vocal isolation failed, so there is no legitimate conditioning audio.

    Exists because the alternative -- quietly conditioning on the full mix -- is
    the documented "mouth follows the beat, not the words" bug, and it produces a
    finished video that looks like a lip-sync FAILURE rather than an error. See
    _isolate_guide_vocals.
    """


def _isolate_guide_vocals(audio_wav, job_dir):
    """Isolate vocals (+150Hz highpass, + phrase gating) for mouth conditioning.

    Returns the guide WAV path. RAISES GuideIsolationError rather than returning
    the full mix.

    HARD-FAIL, 2026-08-04 (LIPSYNC_LEDGER design rule: "the render path must not
    ACCEPT a raw-mix wav -- prep produces a stem artifact, renderers take only
    that"). This function used to return None on five separate paths -- missing
    venv, Demucs failure, any exception, plus two silent DOWNGRADES (a failed
    high-pass kept the unfiltered stem, an empty phrase list kept the ungated
    stem) -- and every one of them landed the caller on `audio_wav`, the raw mix.
    Measured consequence, twice, on two different codebases: slices come out
    bass-dominant (+6.9 dB bass over mid, which an isolated vocal physically
    cannot be), the mouth tracks the drums, and the render is judged as bad
    lip-sync because nothing anywhere says "this was conditioned on the wrong
    audio". Isolating properly moved sync rank 10-26x on the same window.

    A job that cannot isolate vocals must fail loudly and early, before spending
    GPU minutes producing a video whose defect is invisible to every metric.
    """
    from features.lipsync.runner import _paths, _separate_vocals
    _d, _py = _paths()
    if not _py.is_file():
        raise GuideIsolationError(
            "MuseTalk venv not found, so vocals cannot be isolated. Refusing to "
            "condition on the full mix (that renders a mouth that follows the "
            "beat instead of the words). Install/repair the venv, or run this "
            "job with lip_sync off.")
    _voc = str(job_dir / "guide_vocals.wav")
    try:
        _ok = _separate_vocals(_py, audio_wav, _voc)
    except Exception as e:
        raise GuideIsolationError(f"Demucs vocal separation raised: {e}") from e
    if not _ok or not os.path.isfile(_voc):
        raise GuideIsolationError(
            "Demucs vocal separation produced no stem. Refusing to condition on "
            "the full mix.")

    # The high-pass is part of the recipe, not a nicety: it removes the bass/beat
    # bleed the stem still carries. Falling back to the un-high-passed stem is a
    # silent downgrade of the guide, so it is an error too.
    _voc_hp = str(job_dir / "guide_vocals_hp.wav")
    _hp = subprocess.run(
        ["ffmpeg", "-y", "-i", _voc, "-af", "highpass=f=150", _voc_hp],
        capture_output=True, timeout=180,
    )
    if _hp.returncode != 0 or not os.path.isfile(_voc_hp):
        raise GuideIsolationError(
            "150Hz high-pass of the vocal stem failed; the un-filtered stem is "
            "not an acceptable substitute (it keeps the bass bleed the filter "
            "exists to remove).")
    out = _voc_hp

    # Isolating the vocals is not enough: the stem still carries bleed through
    # every instrumental bar, and the conditioning turns that bleed into mouth
    # movement. Silence everything outside the sung phrases so an instrumental
    # clip conditions on real silence and the mouth rests.
    from features.lipsync.vocal_activity import gate_audio, voiced_intervals
    _iv = voiced_intervals(out)
    if _iv:
        _gated = str(job_dir / "guide_vocals_gated.wav")
        if gate_audio(out, _iv, _gated):
            out = _gated
        else:
            log.warning("[song-video] Phrase gating failed -- conditioning on the "
                        "UNGATED stem (still vocals, never the mix)")
    else:
        # NOT an error: a genuinely instrumental track has no phrases, and the
        # correct conditioning for that is the quiet stem (mouth rests). The
        # per-window energy check is what catches the dangerous version of this
        # -- a window that IS sung but whose phrases went undetected.
        log.info("[song-video] No singing detected -- conditioning on the (silent) stem anyway")

    log.info("[song-video] Lip sync: conditioning on isolated vocals (%s)", os.path.basename(out))
    # The intervals go BACK to the caller. They used to be computed here and
    # thrown away, which made the per-window energy check downstream a measured
    # no-op: with no VAD opinion and no plan label to compare against, it could
    # never report a disagreement, so the check written to catch the
    # 79-second undriven-mouth bug could not have detected it. Handing the
    # intervals over is what makes the "VAD under-read this window" branch live.
    return out, _iv


def _build_face_crop(src_path, tw, th, out_path):
    """Crop/zoom src so the face fills the frame with the mouth ~0.66 down. This
    makes mouth motion visible AND puts the mouth where sync_qc expects it (so
    best-of-N can actually rank takes). Returns out_path, or None (no face /
    already a close-up / error) to keep the original framing. Uses OpenCV Haar."""
    try:
        import cv2
        img = cv2.imread(src_path)
        if img is None:
            return None
        ih, iw = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        face = None
        for _c in ("haarcascade_frontalface_alt2.xml",
                   "haarcascade_frontalface_alt.xml",
                   "haarcascade_frontalface_default.xml"):
            det = cv2.CascadeClassifier(cv2.data.haarcascades + _c)
            hits = det.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                        minSize=(int(iw * 0.05), int(ih * 0.05)))
            if len(hits):
                face = max(hits, key=lambda b: b[2] * b[3])
                break
        if face is None:
            return None
        fx, fy, fw, fh = [int(v) for v in face]
        if fh >= 0.55 * ih:
            return None
        aspect = tw / float(th)
        crop_h = min(ih, int(fh / 0.50))
        crop_w = int(crop_h * aspect)
        if crop_w > iw:
            crop_w = iw
            crop_h = int(crop_w / aspect)
        face_cx = fx + fw / 2.0
        mouth_y = fy + 0.88 * fh
        left = max(0, min(int(round(face_cx - crop_w / 2.0)), iw - crop_w))
        top = max(0, min(int(round(mouth_y - 0.66 * crop_h)), ih - crop_h))
        crop = img[top:top + crop_h, left:left + crop_w]
        if crop.size == 0:
            return None
        crop = cv2.resize(crop, (tw, th), interpolation=cv2.INTER_LANCZOS4)
        cv2.imwrite(out_path, crop)
        return out_path if os.path.isfile(out_path) else None
    except Exception as e:
        log.warning("[song-video] face crop failed (%s) -- keeping framing", e)
        return None


def _do_song_gpu_phase(
    job, photo_path, settings, job_dir,
    n_clips, clip_durations, model_name,
    resolution, ow, oh, tw, th, steps, guidance, seed,
    audio_path, audio_dur, story_arc, clip_dur, subject_anchor,
    reanchor_every=3, pad_before=0.0, clip_start_times=None, audio_wav=None,
    keyframes=None, lip_sync_res_active=False,
):
    from app import gallery_push
    from features.fun_videos import video_generator

    # Declared before _log because _log writes to it (closure over this list).
    _last_error: list[str | None] = [None]

    def _log(msg):
        log.info(msg)
        # Keep the last real error the generator reported. video_generator
        # LOGS the worker's message and then returns None, so without this the
        # specific diagnosis (e.g. the worker refusing an audio-conditioned
        # request and naming the resolution/audio-token cause) never reaches
        # the job's final error and everything collapses into the generic
        # "No clips generated" bucket.
        if isinstance(msg, str) and msg.startswith("[error] Generation failed:"):
            _last_error[0] = msg.removeprefix("[error] ").strip()
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

    _chain_frame: str | None = None   # last frame of previous clip -> first frame of next
    _clip_secs: list[float] = []      # per-clip wall-clock times for ETA

    # Lip sync: slice the pre-converted WAV per clip and pass as audio_source.
    # Uses the same -c:a copy slice approach as the Zoom pipeline (known to work).
    # Only enabled when user provided audio that converted successfully in prep.
    _clip_start_times = clip_start_times or []
    _lip_sync = bool(settings.get("lip_sync", True)) and bool(audio_wav) and len(_clip_start_times) == n_clips
    # Face framing is NATIVE-lip_sync only (rolled back 2026-08-03 late: the
    # morning change also triggered it for auto_lipsync -- the DEFAULT path --
    # which silently re-framed Andrew's normal whole-scene music videos into
    # face close-ups. MuseTalk is the wrong sync engine for creature subjects
    # anyway (fundamental paste-box, see LIPSYNC_HANDOFF.md), so widening the
    # crop to its path bought nothing and changed default behavior. Default
    # path now behaves exactly as before 2026-08-03.)
    _want_face_framing = _lip_sync
    _audio_slices_dir = job_dir / "audio_slices"

    # Lip-sync recipe (what actually produces mouth movement, per the DCMVS
    # recipe): frame the source tight on the face (mouth ~0.66 down) and drive
    # the conditioning from isolated VOCALS, not the full mix. Combined with 360p
    # (audio fits) and best-of-N (best_of_n>1 ranks takes by mouth motion), this
    # is what beats the seed lottery. Both pieces degrade gracefully.
    _guide_audio = audio_wav
    _orig_prepped_photo = None
    # Per-clip "this window carries singing" flags, filled by the energy check
    # below. Empty means UNKNOWN (no lip sync, or the check could not run), and
    # unknown must not enforce the sync floor -- see the check's own comment.
    _voiced_window: list[bool] = []
    if _want_face_framing and photo_path and os.path.isfile(photo_path):
        _face = str(job_dir / "face_framed.png")
        if _build_face_crop(photo_path, tw, th, _face):
            _orig_prepped_photo = prepped_photo   # so a later degrade can undo this
            prepped_photo = _face
            log.info("[song-video] Framed source on the face (mouth in lower third)")
    if _lip_sync:
        # THE FALLBACK IS "NO CONDITIONING", NEVER "THE RAW MIX" -- but whether
        # that is an error or a graceful degrade depends on who asked.
        # `lip_sync` DEFAULTS TO TRUE (above), so an ordinary song-video job on
        # a box without the MuseTalk venv reaches this line without anyone
        # having requested lip sync at all. Hard-failing those would break
        # working jobs, which is a worse regression than the bug being fixed.
        # So: an EXPLICIT request fails loudly (the user asked for lip sync and
        # must not be handed a silent statue), while the implicit default drops
        # to an unconditioned render and says so. Neither path ever conditions
        # on the full mix, which is the rule that actually matters.
        _lip_sync_explicit = "lip_sync" in settings
        _guide_intervals: list = []
        try:
            _guide_audio, _guide_intervals = _isolate_guide_vocals(audio_wav, job_dir)
        # Catch Exception, not just GuideIsolationError: isolation reaches into
        # the MuseTalk venv and soundfile, and a plain ImportError or a CUDA OOM
        # inside Demucs is exactly as fatal to an implicit-default job as a
        # clean GuideIsolationError. Catching only our own type left those
        # escaping uncaught and killing the very jobs this branch protects.
        except Exception as e:
            if _lip_sync_explicit:
                raise
            log.warning("[song-video] Lip sync was not explicitly requested and vocal "
                        "isolation is unavailable (%s: %s) -- rendering WITHOUT audio "
                        "conditioning. The mouth will not be driven. Pass "
                        "lip_sync=false to make this explicit, or fix isolation.",
                        type(e).__name__, e)
            _lip_sync = False
            _guide_audio = None
            # prepped_photo was ALREADY replaced with the tight face crop above
            # (framing runs before isolation), and face framing exists only to
            # serve conditioning. Undo it, or an unconditioned render ships as a
            # face close-up -- the exact default-behaviour change the
            # 2026-08-03 rollback comment above was written to prevent.
            _want_face_framing = False
            if _orig_prepped_photo is not None:
                prepped_photo = _orig_prepped_photo

        # The window rule, mechanised (LIPSYNC_LEDGER 2026-08-04): measure each
        # planned window's energy on the ISOLATED stem and condition anything
        # above the floor, whatever the plan called it. Measurement only -- it
        # never silences a window the plan wanted sung. Skipped entirely when we
        # degraded above: there is no stem to measure, and "checked it, found
        # nothing" must not be mistaken for "there was nothing to find".
        try:
            if _guide_audio is None:
                raise RuntimeError("no vocal stem (unconditioned render)")
            from features.song_video.window_energy import check_plan
            # `intervals` is what makes this check able to say anything at all:
            # without a VAD opinion AND without a plan label, every disagreement
            # branch is unreachable and the whole pass is a no-op that logs
            # nothing while looking like a safeguard.
            _wins = [{"t0": float(_st),
                      "t1": float(_st) + max(4.0, min(12.0, float(
                          _arc.get("duration", clip_dur) if isinstance(_arc, dict) else clip_dur))),
                      "labelled_sung": None,
                      "intervals": _guide_intervals}
                     for _st, _arc in zip(_clip_start_times, story_arc)]
            _we = check_plan(_guide_audio, _wins, log=lambda m: log.info("[song-video] %s", m))
            # The measurement now DRIVES acceptance, not just the log. A window
            # that carries singing is held to SYNC-OR-DIE below; one that does
            # not may bank a clean static take, because a resting mouth through
            # an instrumental bar is the correct content and burning takes
            # chasing a "synced" verdict on silence is pure waste.
            _voiced_window = [bool(w.get("must_condition")) for w in _we]
            log.info("[song-video] sync-or-die applies to %d/%d windows "
                     "(the rest are instrumental and may rest the mouth)",
                     sum(_voiced_window), len(_voiced_window))
        except Exception as e:
            # The LOG is advisory; the ACCEPTANCE decision is not. If the
            # measurement could not run we cannot tell singing from silence, so
            # fall back to not enforcing the floor -- enforcing it blindly would
            # fail instrumental windows that are correctly static, which is a
            # worse failure than the one being guarded against.
            log.warning("[song-video] window energy check skipped (%s) -- sync floor "
                        "NOT enforced this run (cannot tell voiced from instrumental)", e)
            _voiced_window = []

    # Pre-extract ALL audio slices before the clip generation loop starts.
    # This runs once upfront so WanGP never waits for an ffmpeg subprocess
    # between clips. Each slice: corrected start time + clip duration from WAV.
    _audio_slices: list[str | None] = [None] * n_clips
    if _lip_sync:
        _audio_slices_dir.mkdir(exist_ok=True)
        log.info("[song-video] Lip sync ON -- pre-extracting %d audio slices", n_clips)
        for _si, (_st, _arc) in enumerate(zip(_clip_start_times, story_arc)):
            _sdur = float(_arc.get("duration", clip_dur) if isinstance(_arc, dict) else clip_dur)
            # Clamp identically to `this_dur` below (the ACTUAL rendered clip
            # duration) -- the LLM-produced per-clip "duration" is asked for
            # 5-10s in the arc-generation prompt but nothing enforces that
            # range before it got here. Without this clamp, a clip whose arc
            # duration falls outside [4, 12] got a pre-cut lip-sync guide
            # vocal slice of a DIFFERENT length than what WanGP actually
            # renders for that clip -- real audio/video desync on that clip
            # (the mouth-conditioning audio runs short or long relative to
            # the video).
            _sdur = max(4.0, min(12.0, _sdur))
            _sp = str(_audio_slices_dir / f"slice_{_si:02d}.wav")
            _sr = subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{_st:.4f}", "-t", f"{_sdur:.4f}",
                 "-i", _guide_audio, "-c:a", "copy", _sp],
                capture_output=True, timeout=30,
            )
            if _sr.returncode == 0 and Path(_sp).exists():
                _audio_slices[_si] = _sp
        log.info("[song-video] Audio slices ready: %d/%d", sum(1 for s in _audio_slices if s), n_clips)
    elif settings.get("lip_sync") and not audio_wav:
        log.warning("[song-video] Lip sync requested but audio WAV conversion failed -- skipping")
    elif settings.get("lip_sync") and len(_clip_start_times) != n_clips:
        log.warning("[song-video] Lip sync skipped -- clip_start_times length %d != n_clips %d", len(_clip_start_times), n_clips)

    # Best-of-N seed selection (opt-in via best_of_n; default 1 = off). Needs the
    # audio<->mouth sync QC to rank takes; degrades gracefully if unavailable.
    _best_of_n = max(1, int(settings.get("best_of_n", 1) or 1))
    try:
        from features.song_video import sync_qc as _sync_qc
    except Exception:
        _sync_qc = None
    if _best_of_n > 1 and _sync_qc is None:
        log.warning("[song-video] best_of_n=%d requested but sync_qc unavailable -- single-take", _best_of_n)
        _best_of_n = 1
    if _best_of_n > 1:
        log.info("[song-video] best-of-%d seed selection ON (ranked by audio<->mouth sync)", _best_of_n)

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
            # For chained clips (1+), pull every clip toward the source photo
            # at its END so identity is continuously anchored without a visible
            # "reset" cut. Without this, 5 chain hops were enough for a
            # stylized propaganda-poster cat to drift into a photoreal tabby in
            # a leather jacket. Clip 0 keeps end_image=None because its start
            # is already the source -- start=end would force a Ken Burns zoom.
            clip_end_image   = prepped_photo if _chain_frame else None
            _is_chained = bool(_chain_frame)

        if _lip_sync:
            # DCMVS's proven recipe runs LTX-2's native SE (start+end keyframe) mode
            # for EVERY lip-sync clip, including clip 0 -- "every clip ends on the
            # original frame" (locked subject, seamless hard-cut joins). The
            # "Ken Burns zoom" concern above is about the general story-arc case,
            # where progression is wanted; for lip-sync the opposite is wanted (the
            # subject should stay put, only the mouth should move), so SE mode is
            # correct here even on clip 0. Root-caused 2026-07-29: without this,
            # the same seed/prompt/audio that clears the mouth-sync gate on DCMVS
            # produces a near-static, unsynced clip through this pipeline --
            # confirmed via matched raw-payload A/B testing (see LIPSYNC_HANDOFF_
            # 2026-07-29_night.md), not a seed-lottery or worker-instance issue.
            #
            # 2026-08-04 (Tier-1 port, verify-after-driver-rollback): NO CHAINING
            # on the lip-sync path at all -- start from the PRISTINE source every
            # clip, exactly DCMVS's SECTION_CLIPS=1 + SE_END_ANCHOR=True shipped
            # default. Chaining re-encodes an AI-reinterpreted frame each hop and
            # DCMVS's own milestones call the result "not watchable"; with SE the
            # boundaries are near-identical frames so hard cuts read as seamless.
            # (Without this, end=start above anchored to the DRIFTED chain frame,
            # not the source -- half the fix.)
            clip_start_image = prepped_photo
            _is_chained = False
            clip_end_image = clip_start_image

        prompt_to_use = clip_prompt
        from features.fun_videos.multi_pipeline import _strip_camera_moves
        if _lip_sync:
            # PROVEN working lip-sync prompt, taken from the user's own
            # .recipe.json files (DCMVS) that produced perfectly mouth-synced
            # clips: the subject + a direct mouth-sync + STEADY-framing directive,
            # with NO story-arc / motion / camera content. That story-arc content
            # (added during the consolidation into Studio) is exactly what made the
            # model move the CAMERA instead of the MOUTH. subject_anchor is
            # prepended just below to keep the character identity.
            prompt_to_use = ("centered and alone in the frame, facing the camera, "
                             "mouth opening and closing in precise sync with the singing vocals, "
                             "the only subject in the shot, no other people and no other faces, "
                             "plain dark background, soft cinematic key light, "
                             "shallow depth of field, steady framing")
        else:
            # Strip explicit camera direction words (zoom, pan, dolly) but do NOT
            # lock to "static shot" -- that suppresses all motion.
            prompt_to_use = _strip_camera_moves(prompt_to_use)

        # Prepend subject anchor so every clip is grounded to the actual photo.
        if subject_anchor and not prompt_to_use.lower().startswith(subject_anchor[:20].lower()):
            prompt_to_use = subject_anchor + " " + prompt_to_use

        # Do NOT add "exact same location" prefix -- it suppresses all motion.
        # Identity is maintained by the Forge keyframe start_image and subject_anchor text.

        if not prompt_to_use.strip():
            prompt_to_use = subject_anchor or "Subject in atmospheric scene, natural movement, cinematic"
        if _lip_sync:
            # Match the recipe exactly: NO quality/motion suffix -- those add
            # "calm/subtle motion" or "dynamic" cues that fight the mouth-sync.
            # Just the subject + mouth-sync + steady-framing directive.
            finalized = prompt_to_use
        else:
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

        def _gen_one(_seed, _out):
            return video_generator.generate_video(
                image_path=clip_start_image,
                prompt=finalized,
                out_path=_out,
                duration=this_dur,
                model_name=model_name,
                resolution=resolution,
                override_width=int(tw) if (ow and oh) or lip_sync_res_active else None,
                override_height=int(th) if (ow and oh) or lip_sync_res_active else None,
                steps=steps,
                guidance=effective_guidance,
                seed=_seed,
                end_image_path=clip_end_image,
                negative_prompt=("blurry, distorted" if _lip_sync else video_generator.negative_prompt_for(model_name, motion_style="narrative")),
                audio_source=_audio_slice,
                # DCMVS-proven conditioning strength -- WanGP defaults to 1.0 when
                # unset, which over-drives the mouth region hard enough to visibly
                # degrade detail there (the "box" artifact). Gated on _audio_slice,
                # not the job-level _lip_sync flag: if this specific clip's slice
                # extraction failed, it has no audio conditioning regardless of the
                # job setting, and input_video_strength=0.69 (looser than the 1.0
                # default) with nothing driving the motion just lets the subject
                # drift off the source image for no benefit.
                audio_scale=(0.6 if _audio_slice else None),
                input_video_strength=(0.69 if _audio_slice else None),
                stop_check=_stopped,
                log_fn=_log,
                progress_fn=_video_progress,
                worker_url=_worker_url or None,
            )

        try:
            # Best-of-N: LTX-2 at 8 steps is a seed lottery -- the same recipe
            # lands audio-driven motion on the mouth vs the eyes depending on the
            # seed. When best_of_n>1 AND this clip has audio to sync to, generate
            # several seeds and keep the highest audio<->mouth sync score.
            # best_of_n=1 (default) -> single generation, identical to before.
            if _best_of_n > 1 and _audio_slice and _sync_qc is not None and not _use_keyframes:
                # SYNC-OR-DIE only where the stem says someone is singing.
                _needs_sync = bool(_voiced_window[i]) if i < len(_voiced_window) else False
                clip_path = _pick_best_seed(i, clip_out, _gen_one, _audio_slice,
                                            seed, _best_of_n, log_fn=_log,
                                            require_sync=_needs_sync)
            else:
                clip_path = _gen_one(seed, clip_out)
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
            # RECORD WHY. This branch previously left _last_error[0] as None, so
            # any generator failure that returned None instead of raising -- the
            # worker's own explanatory refusals included -- was laundered into
            # the job's generic "No clips generated -- check WanGP is running".
            # That string is the ledger's standing unsolved item (~23% of jobs,
            # no root cause on record) precisely because it is where specific
            # diagnoses go to die. Keep whatever the generator last said.
            _last_error[0] = _last_error[0] or (
                f"Clip {clip_num} produced no output. If the job is audio-conditioned, "
                f"check the worker log for a refusal (resolution vs audio-token budget) "
                f"before assuming WanGP is down.")
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
                        override_width=int(tw) if (ow and oh) or lip_sync_res_active else None,
                        override_height=int(th) if (ow and oh) or lip_sync_res_active else None,
                        steps=steps,
                        guidance=effective_guidance,
                        seed=seed,
                        end_image_path=clip_end_image,
                        negative_prompt=("blurry, distorted" if _lip_sync else video_generator.negative_prompt_for(model_name, motion_style="narrative")),
                        audio_source=_audio_slice,
                        audio_scale=(0.6 if _audio_slice else None),
                        input_video_strength=(0.69 if _audio_slice else None),
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
            # Retry succeeded -- fall through to the normal per-clip processing
            # below (trim/chain-frame/upscale/append) instead of discarding it.

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

        # POX-MARK FIX (2026-06-19): extract the chain frame for the NEXT clip
        # from the clip BEFORE the lossy per-clip upscale below. The boundary
        # trims above are all `-c copy` (lossless), but the upscale re-encodes at
        # libx264 CRF 18 and overwrites clip_path in place; reading the
        # conditioning frame from that re-encoded file baked H.264/VAE compression
        # into the next clip's seed, and the diffusion model re-synthesized +
        # amplified it on every hop -> cumulative skin "pox marks" as the video
        # progresses. Capture the clean (pre-upscale) frame first; the upscale then
        # runs only on the copy used for the final concat, never fed into the chain.
        # (This mirrors the documented contract in multi_pipeline._chain_anchor.)
        if i < n_clips - 1:
            _cframe_path = str(job_dir / f"chain_{i:02d}.png")
            _chain_frame = _extract_last_frame(clip_path, _cframe_path)
            if not _chain_frame:
                log.info("[song-video] Frame extraction failed for clip %d -- next clip uses source", clip_num)
            else:
                log.info("[song-video] Clip %d chain frame (pre-upscale, clean): %s", clip_num, _cframe_path)

            if reanchor_every > 0 and (i + 1) % reanchor_every == 0:
                log.info("[song-video] Re-anchoring clip %d to source photo (every %d)", i + 2, reanchor_every)
                _chain_frame = None

        # Per-clip upscale when lip sync forced 360p generation. This re-encodes
        # (CRF) and overwrites clip_path; it now runs AFTER the chain frame is
        # already captured above, so its compression can never enter the chain.
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

    # A SyncFloorNotMet (or any clip exception) breaks the render loop, which
    # SHORTENS the delivered video to whatever synced before the failure --
    # this codebase's existing philosophy that N clips beats zero, now paired
    # with the new rule that a clip must never be a statue. That is a real,
    # visible product outcome (a "60s" video that plays 30s), and it must not
    # be reported as an unremarkable "complete". Record it in the job meta so
    # the truncation and its cause survive past this function, not just the log.
    if len(clip_paths) < n_clips:
        job.meta["clips_requested"] = n_clips
        job.meta["truncated_reason"] = _last_error[0] or "unknown"
        _log(f"[warning] Delivering {len(clip_paths)}/{n_clips} clips -- stopped "
             f"early ({job.meta['truncated_reason']})")

    job.meta["stage"] = "concatenating"
    job.update(progress=79, message=f"Concatenating {len(clip_paths)} clips...")
    job.meta["clips_generated"] = len(clip_paths)

    # -- Phase 2: Concat with xfade transitions
    # Check every clip is still on disk right before merge -- a clip that
    # vanished after being generated (seen at least once, root cause not
    # pinned down -- see SESSION_HANDOFF_2026-08-02_evening.md) otherwise
    # surfaces only as an opaque ffmpeg concat failure with no indication
    # of which file, or which clip index, was actually missing.
    missing = [p for p in clip_paths if not os.path.isfile(p)]
    if missing:
        raise RuntimeError(
            f"Song video failed: {len(missing)}/{len(clip_paths)} clip file(s) "
            f"went missing before merge (generated but no longer on disk): "
            f"{', '.join(missing)}"
        )

    # Collect per-clip durations for correct xfade offset math.
    clip_durations = [probe_duration(p) or 4.0 for p in clip_paths]
    concat_path = str(job_dir / f"concat_{job.id[:6]}.mp4")
    _xfade_dur = settings.get("_song_xfade_dur", 0.75)
    if not _concat_with_xfade(clip_paths, clip_durations, concat_path, fade_dur=_xfade_dur):
        log.warning("[song-video] xfade concat failed -- falling back to hard cut")
        if not _concat_clips(clip_paths, concat_path):
            raise RuntimeError(
                f"Song video failed: could not concatenate {len(clip_paths)} clips "
                f"(both xfade and hard-cut concat failed). Clip files are in {job_dir}"
            )

    effective_dur = audio_dur if audio_dur > 0 else (probe_duration(audio_path) or 0.0)
    if effective_dur <= 0:
        raise RuntimeError("Cannot determine audio duration -- file may be missing or corrupt")

    # Beat-sync DTW pass removed: clips are already beat-aligned by the pipeline
    # (clip durations snap to beat boundaries during analysis). Running DTW again
    # adds 2-3 min of pure overhead per video with no quality benefit.
    video_to_loop = concat_path

    # -- Phase 3: Loop to fill song + merge audio -----------------------------
    job.meta["stage"] = "merging"
    job.update(progress=92, message="Fitting video to song and merging audio...")

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

        # -- Lip sync post-pass (MuseTalk) ------------------------------------
        # MuseTalk is human-face-trained: on real human/humanoid faces it works,
        # on stylized cartoon-animal faces (e.g. propaganda-poster cats) it
        # places a smeared inpaint blob across the mouth region -- worse than
        # no lip sync. Gated behind an EXPLICIT opt-in (`auto_lipsync`,
        # default False) so the human-face case can be enabled per-job, while
        # the default music-video output stays clean. The manual "Lip-sync to
        # audio" button on the Queue/Gallery detail still works case-by-case.
        if bool(settings.get("auto_lipsync", False)) and not _stopped():
            try:
                from features.lipsync.runner import NoVocalsError, lipsync_available, lipsync_video
                if lipsync_available():
                    job.meta["stage"] = "lip-sync"
                    job.update(progress=96, message="Lip-syncing to the words (MuseTalk)...")
                    ls_out = merged.replace(".mp4", "_ls.mp4")
                    try:
                        synced = lipsync_video(job, merged, audio_path, ls_out, isolate_vocals=True)
                    except NoVocalsError as _nv:
                        # Not a failure: an instrumental track has nothing to sync.
                        synced = None
                        job.meta["lipsync_skipped"] = str(_nv)
                        log.info("[song-video] Lip sync skipped -- %s", _nv)
                    if synced and os.path.isfile(synced):
                        if synced != merged:
                            try:
                                os.remove(merged)
                            except Exception:
                                pass
                        merged = synced
                        log.info("[song-video] Lip sync applied: %s", Path(synced).name)
                else:
                    job.meta["lipsync_error"] = "MuseTalk is not installed"
                    log.info("[song-video] Lip sync requested but MuseTalk not installed -- skipping")
            except Exception as _ls:
                # The video is still usable, but it has NO word-level sync -- say so
                # rather than shipping a beat-synced video that looks like a bug.
                job.meta["lipsync_error"] = str(_ls)
                log.warning("[song-video] Lip sync post-pass failed (keeping un-synced video): %s", _ls)

        job.output = merged
        from core.inbox import copy_to_inbox; copy_to_inbox(job.output)
        job.meta.update({"final_path": merged, "audio_path": audio_path})
        job.message = f"Music video complete! ({len(clip_paths)} clips)"
        if job.meta.get("lipsync_error"):
            job.message += " -- but lip sync failed, so the mouth is not synced to the words"
        elif job.meta.get("lipsync_skipped"):
            job.message += " -- instrumental track, so no lip sync"
        if job.meta.get("subject_warning"):
            job.message += (" -- WARNING: the source image had no face or figure, "
                            "so expect drifting scenery instead of a performance")

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
