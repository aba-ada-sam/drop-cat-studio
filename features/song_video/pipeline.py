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
            "Describe the main subject in 20-25 words. "
            "Include: exact hair color and style, clothing color and type, "
            "skin tone, eye color, distinguishing features, species if not human, "
            "any accessories. Visual details only, no emotions or context. "
            "Example: 'Red-haired woman, loose shoulder curls, blue denim jacket, "
            "white t-shirt, pale freckled skin, hazel eyes, small silver earrings.'",
            [b64], tier=TIER_FAST, max_tokens=90,
        )
        # Strip any markdown the LLM adds (headers, bullets, bold, etc.)
        import re as _re
        anchor = raw.strip()
        anchor = _re.sub(r'^#+\s*[^\n]*\n+', '', anchor)   # strip # headings
        anchor = _re.sub(r'\*\*([^*]+)\*\*', r'\1', anchor) # **bold**
        anchor = _re.sub(r'\*([^*]+)\*', r'\1', anchor)     # *italic*
        anchor = anchor.strip().strip('"').strip("'").split('\n')[0].strip()
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

ONE MOTION PER CLIP with start + end position:
  Good: "right arm rises from hip to shoulder height over 3 seconds"
  Good: "sleeve fabric ripples at wrist then settles still"
  Good: "shoulders rise with one slow breath, then fall back"
  Bad: "moves with dramatic energy" (vague -- model produces static output)
  Bad: "walks across the scene" (too complex -- loses identity at 8 steps)

Match energy to song section:
  LOW energy (verse, intro): micro-motion -- hand shifts 3cm, fabric stirs
  MED energy (pre-chorus): body periphery -- shoulder rise, arm lift
  HIGH energy (chorus, drop): single bold action -- arm raises fully, body leans

BANNED (causes artifacts or style drift):
  anime, cartoon, 2D, ethereal, mystical, otherworldly, blazing, transcendent
  zoom, pan, push, pull, dolly, tilt (camera moves -- use sparingly, ONE per clip max)
  dust, sparks, smoke, fog, bokeh, confetti (particle artifacts)
  multiple simultaneous actions

Return ONLY valid JSON:
{"clips": [{"prompt": "...", "duration": 7}, {"prompt": "...", "duration": 8}]}
Duration: seconds per clip (5-10). Match song section lengths.\
"""


def _generate_song_arc(
    llm_router,
    n_clips: int,
    analysis: dict,
    user_idea: str,
    photo_path: str | None,
    variety_theme: str = "",
    lyrics_text: str = "",
) -> list[str]:
    """Generate N motion prompts that follow a single story across the song.

    Beat alignment is handled in post-generation by the motion analyzer +
    speed ramp -- the LLM only needs to ensure each clip has one clear visual
    climax, not time it precisely.
    """
    clip_labels = analysis.get("clip_energy_labels", [])
    bpm    = analysis.get("bpm")
    key    = analysis.get("key", "")
    mode   = analysis.get("mode", "")
    mood   = analysis.get("mood", "")

    # Per-clip pacing label only -- no percentages, no beat-timing instructions.
    # The label hints at narrative intensity (intro / climb / peak / release)
    # without dictating motion adjectives.
    clip_hints = []
    for i in range(n_clips):
        label = clip_labels[i] if i < len(clip_labels) else "MED"
        clip_hints.append(f"Clip {i + 1:02d}: {label} energy section")

    energy_text = "\n".join(clip_hints)
    key_str = f"{key} {mode}".strip() if key else ""
    bpm_str = f"{bpm} BPM" if bpm else ""
    song_desc = ", ".join(filter(None, [key_str, bpm_str, mood]))

    story_direction = (user_idea or "").strip() or "a music video that visually matches the song's mood and energy"
    style_line  = f"Visual style / aesthetic: {variety_theme}\n" if variety_theme else ""
    lyrics_line = (
        f"\nSong lyrics / theme (first ~2 min):\n{lyrics_text[:800].strip()}\n"
        if lyrics_text and lyrics_text.strip() else ""
    )

    user_msg = (
        f"Song character: {song_desc or 'dynamic track'}\n"
        f"Story direction: {story_direction}\n"
        f"{style_line}"
        f"{lyrics_line}"
        f"\nNarrative pacing per clip ({n_clips} clips):\n{energy_text}\n\n"
        f"Generate exactly {n_clips} motion prompts that continue the SAME story "
        f"and follow this pacing."
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

    # Run beat alignment + lyric detection concurrently -- both CPU, no GPU needed.
    job.meta["stage"] = "analyzing"
    job.meta["clips_total"] = n_clips
    job.update(progress=2, message="Analysing beat structure and detecting lyrics...")
    from features.song_video.audio_analyzer import compute_clip_plan, _transcribe_lyrics

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        # max_dur must be >= min_dur (clip_dur); cap at 10s for quality but never below clip_dur
        fut_beats  = pool.submit(compute_clip_plan, audio_path, n_clips, clip_dur, max(float(clip_dur), 10.0))
        fut_lyrics = pool.submit(_transcribe_lyrics, audio_path) if (not lyrics_text and os.path.isfile(audio_path)) else None
        # Subject anchor runs concurrently -- vision LLM call, adds ~0 wall time.
        fut_anchor = pool.submit(_extract_subject_anchor, photo_path, llm_router) if photo_path else None
        # Beat events for snap pass -- runs concurrently, CPU only.
        from features.fun_videos.audio_analyzer import detect_audio_events
        fut_events = pool.submit(detect_audio_events, audio_path) if os.path.isfile(audio_path) else None

        clip_durations, beat_positions = fut_beats.result()
        if fut_lyrics is not None:
            detected = fut_lyrics.result()
            if detected:
                lyrics_text = detected
                log.info("[song-video] Auto-detected %d chars of lyrics", len(lyrics_text))
        subject_anchor = fut_anchor.result() if fut_anchor else ""
        if subject_anchor:
            log.info("[song-video] Subject anchor: %s", subject_anchor[:80])
        audio_events = fut_events.result() if fut_events else {}

    # Guard: if the song is shorter than n_clips * min_dur, _place_boundaries
    # collapses trailing boundaries to total_dur, producing zero-duration clips.
    # ffmpeg -t 0 then writes an empty file and all subsequent clips fail.
    clip_durations = [max(8.0, min(10.0, d)) for d in clip_durations]
    settings["_clip_durations"] = clip_durations
    settings["_beat_positions"] = beat_positions
    log.info("[song-video] Clip durations (beat-aligned): %s", clip_durations)
    log.info("[song-video] Beat positions per clip: %s", beat_positions)

    # Compute the start time of each clip within the song (for per-clip audio conditioning).
    # pad_before is the silence before the song starts; each clip then follows in sequence.
    _start_t = float(settings.get("pad_before", 1.0))
    _clip_start_times: list[float] = []
    for _d in clip_durations:
        _clip_start_times.append(_start_t)
        _start_t += float(_d)
    settings["_clip_start_times"] = _clip_start_times

    settings["_subject_anchor"] = subject_anchor

    job.meta["stage"] = "planning"
    job.update(progress=4, message="Planning music video story arc...")
    try:
        arc = _generate_song_arc(llm_router, n_clips, analysis, user_idea, photo_path, variety_theme, lyrics_text)
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

    # Pure chain: never reset to source photo. Hard resets create the "same image"
    # jump cut. Identity is maintained via subject_anchor text in every prompt.
    reanchor_every = 0
    settings["_reanchor_every"] = reanchor_every
    log.info("[song-video] reanchor_every=%d (from %d sections)", reanchor_every, len(audio_events.get("sections", []) if audio_events else []))

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

    if not story_arc:
        story_arc = [settings.get("video_prompt", "") or "Subject erupts into motion"] * n_clips
    if not audio_path or not os.path.isfile(audio_path):
        raise RuntimeError("Audio file not found -- please re-upload the song")

    ts      = time.strftime("%Y-%m-%d")
    slug    = Path(photo_path).stem[:14].replace(" ", "_") if photo_path else "songvid"
    job_dir = OUTPUT_DIR / ts / f"songvid_{slug}_{job.id}"
    job_dir.mkdir(parents=True, exist_ok=True)

    # Output dimensions
    if ow and oh:
        tw, th = int(ow), int(oh)
    else:
        _native = video_generator.MODELS.get(model_name, {}).get("res") or (1032, 580)
        tw, th = _native

    if photo_path and os.path.isfile(photo_path):
        shutil.copy2(photo_path, job_dir / f"source{Path(photo_path).suffix}")

    # -- GPU: acquire WanGP exclusively (orchestrator evicts everything else)
    from core.gpu_orchestrator import gpu
    gpu.acquire("wangp", reason=f"song-video {n_clips} clips")
    _do_song_gpu_phase(
        job, photo_path, settings, job_dir,
        n_clips, clip_durations, beat_positions, model_name,
        resolution, ow, oh, tw, th, steps, guidance, seed,
        audio_path, audio_dur, story_arc, clip_dur, subject_anchor,
        reanchor_every=reanchor_every, pad_before=pad_before,
        clip_start_times=clip_start_times,
    )
    # Orchestrator keeps WanGP loaded; next acquire of a different service evicts.


def _do_song_gpu_phase(
    job, photo_path, settings, job_dir,
    n_clips, clip_durations, beat_positions, model_name,
    resolution, ow, oh, tw, th, steps, guidance, seed,
    audio_path, audio_dur, story_arc, clip_dur, subject_anchor,
    reanchor_every=3, pad_before=0.0, clip_start_times=None,
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

    _last_error: list[str | None] = [None]
    _chain_frame: str | None = None   # last frame of previous clip -> first frame of next
    _clip_secs: list[float] = []      # per-clip wall-clock times for ETA

    # Audio conditioning disabled pending investigation -- WanGP rejects
    # audio_prompt_type=A even for ltx2_distilled when using extracted WAV slices.
    # TODO: debug correct audio format/duration requirements, then re-enable.
    _lip_sync = False
    _audio_slices_dir = job_dir / "audio_slices"
    _clip_start_times = clip_start_times or []

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
        clip_start_image = _chain_frame if _chain_frame else prepped_photo
        _is_chained = bool(_chain_frame)

        prompt_to_use = clip_prompt
        # Strip camera moves and lock to static shot so LTX doesn't default to zoom/pan
        from features.fun_videos.multi_pipeline import _enforce_static_camera
        prompt_to_use = _enforce_static_camera(prompt_to_use)

        # Prepend subject anchor so every clip is grounded to the actual photo.
        if subject_anchor and not prompt_to_use.lower().startswith(subject_anchor[:20].lower()):
            prompt_to_use = subject_anchor + " " + prompt_to_use

        # For chained clips, tell the model to continue from the anchor frame.
        if _is_chained:
            prompt_to_use = "Exact same location and subject as previous frame, continuous scene. " + prompt_to_use

        if not prompt_to_use.strip():
            prompt_to_use = subject_anchor or "Subject in atmospheric scene, natural movement, cinematic"
        finalized = _finalize_prompt(prompt_to_use, model_name, motion_style="narrative")
        if not finalized.strip():
            finalized = "Cinematic scene, natural movement, photorealistic, high quality"
        clip_out  = str(job_dir / f"clip_{i:02d}_{job.id[:6]}.mp4")
        _arc_dur  = _arc_entry.get("duration") if isinstance(_arc_entry, dict) else None
        this_dur  = float(_arc_dur) if _arc_dur else (clip_durations[i] if i < len(clip_durations) else clip_dur)
        this_dur  = max(4.0, min(12.0, this_dur))

        # Chained clips: reduce guidance so the anchor frame dominates over text.
        # Clip 0 (source photo) keeps full guidance -- identity is ground truth.
        if not _is_chained:
            effective_guidance = min(guidance, 3.5)
        elif "distilled" in model_name.lower():
            effective_guidance = min(guidance, 2.8)
        elif "ltx" in model_name.lower():
            effective_guidance = min(guidance, 3.0)
        else:
            effective_guidance = min(guidance, 4.0)

        # Extract the audio segment for this clip's time window so LTX-2 can
        # condition the video generation directly on the music. WAV avoids MP3
        # seek-boundary artifacts that would give LTX-2 a misaligned audio window.
        _audio_slice: str | None = None
        if _lip_sync and audio_path and os.path.isfile(audio_path) and i < len(_clip_start_times):
            _slice_path = str(_audio_slices_dir / f"slice_{i:02d}.wav")
            _sr = subprocess.run(
                ["ffmpeg", "-y", "-i", audio_path,
                 "-ss", f"{_clip_start_times[i]:.4f}",
                 "-t",  f"{this_dur:.4f}",
                 "-ar", "44100", "-ac", "1", _slice_path],
                capture_output=True, timeout=30,
            )
            if _sr.returncode == 0 and Path(_slice_path).exists():
                _audio_slice = _slice_path
            else:
                log.debug("[song-video] Audio slice extraction failed for clip %d -- generating without audio conditioning", clip_num)

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

        # LTX-2 bakes a ~0.2s fade-out into the tail of every clip.
        # Trim intermediate clips so the boundary frame is in-motion.
        # Leave the last clip untrimmed so the video ends with a natural fade.
        if i < n_clips - 1:
            clip_real_dur = probe_duration(clip_path) or this_dur
            trim_to = max(clip_real_dur - 0.2, clip_real_dur * 0.9)
            tail_out = str(job_dir / f"clip_{i:02d}_fe.mp4")
            tr = subprocess.run(
                ["ffmpeg", "-y", "-i", clip_path, "-t", f"{trim_to:.4f}", "-c", "copy", tail_out],
                capture_output=True, timeout=60,
            )
            if tr.returncode == 0 and Path(tail_out).exists():
                os.replace(tail_out, clip_path)
            else:
                log.debug("[song-video] Clip %d tail-trim failed -- using full clip", clip_num)

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
    if not _concat_with_xfade(clip_paths, clip_durations, concat_path, fade_dur=0.5):
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
        # Fast mode generates 360P for speed then upscales to 720P.
        if ow and oh and int(oh) <= 360 and not _stopped():
            job.update(progress=97, message="Upscaling fast-mode video to 720P...")
            try:
                from core.upscaler import upscale_video
                up_path = merged.replace(".mp4", "_720p.mp4")
                up_out, up_err = upscale_video(merged, up_path, scale=2.0, method="ffmpeg")
                if up_out and Path(up_out).exists():
                    merged = up_out
                    log.info("[song-video] Fast-mode upscale: %s -> %s", Path(up_path).name, up_out)
                else:
                    log.warning("[song-video] Upscale failed (%s) -- keeping 360P output", up_err)
            except Exception as _ue:
                log.warning("[song-video] Upscale exception: %s -- keeping 360P", _ue)

        job.output = merged
        from core.inbox import copy_to_inbox; copy_to_inbox(job.output)
        job.meta.update({"final_path": merged, "audio_path": audio_path})
        job.message = f"Music video complete! ({len(clip_paths)} clips)"

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
