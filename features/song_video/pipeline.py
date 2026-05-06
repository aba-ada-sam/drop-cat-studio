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
from core.llm_client import TIER_BALANCED, encode_image_b64, parse_json_response
from features.fun_videos.pipeline import _prep_photo, _finalize_prompt
from features.fun_videos.multi_pipeline import _concat_clips
from features.song_video.motion_analyzer import align_clip_to_beat

log = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"


def _extract_audio_segment(audio_path: str, start_time: float, duration: float, out_path: str) -> str | None:
    """Cut [start_time, start_time+duration] from audio as a stereo 44100 Hz WAV.

    Transcoding to WAV avoids MP3 frame-boundary issues when seeking, and gives
    LTX-2 a format it can reliably decode with torchaudio.
    """
    r = subprocess.run(
        ["ffmpeg", "-y",
         "-ss", f"{start_time:.4f}", "-t", f"{duration:.4f}",
         "-i", audio_path,
         "-ar", "44100", "-ac", "2", "-acodec", "pcm_s16le",
         out_path],
        capture_output=True, timeout=30,
    )
    return out_path if r.returncode == 0 and Path(out_path).exists() else None


def _extract_last_frame(video_path: str, out_path: str) -> str | None:
    """Extract the actual last frame of a video as a JPEG.

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
             "-frames:v", "1", "-q:v", "2", out_path],
            capture_output=True, timeout=30,
        )
    else:
        r = subprocess.run(
            ["ffmpeg", "-y", "-sseof", "-0.1", "-i", video_path,
             "-frames:v", "1", "-q:v", "2", out_path],
            capture_output=True, timeout=30,
        )
    return out_path if (r.returncode == 0 and Path(out_path).exists()) else None


# -- Story arc generation ------------------------------------------------------

_SONG_ARC_SYSTEM = """\
You write motion prompts for an image-to-video AI generating a music video.
Each prompt is one 8-20 second clip. All clips together tell ONE coherent story.

WORLD RULE: Clip 01 must establish a SPECIFIC, NAMED setting.
Name the location, the material, the light source, and the subject.
Good: "A woman in a red coat on a rain-slick Tokyo street under sodium lamps"
Bad: "a figure in a dramatic landscape bathed in ethereal light"
Every subsequent clip stays in this world. Same subject, same place, different
MOMENT or CAMERA POSITION. Never teleport to a new environment between clips.

ANTI-SLOP RULE: Every word must earn its place.
Banned words and phrases: blazing, sweeping, ethereal, cinematic, dramatic,
luminous, breathtaking, majestic, haunting, mesmerizing, pulsing with energy,
bathed in light, awash in color, transcendent, otherworldly.
Replace vague adjectives with PHYSICAL SPECIFICS:
  NOT "blazing fire" -- WRITE "orange flames eating a wooden chair leg"
  NOT "dramatic shadows" -- WRITE "hard ceiling light casting black bar shadows on concrete"
  NOT "sweeping landscape" -- WRITE "flat wheat field stretching to a grey horizon"

CAMERA MOTION RULE -- every clip must have exactly ONE purposeful camera MOVE:
  zoom out (pull back to reveal), zoom in (push toward subject), slow pan left/right,
  dolly forward (glide through environment), tilt up (sweep from ground to sky),
  orbit/arc (camera circles subject), crane up, drift (slow float).
The move must be physically motivated by the story moment. Do NOT describe static shots.
Each move evolves from the previous clip's final frame since clips are chained.

MOTION ARC RULE -- every clip must have ONE clear visual climax -- a single moment of
maximum action -- not uniform motion throughout:
  - quiet build -> dolly accelerates -> object ARRIVES or IMPACTS
  - subject moves through space -> REACHES a defined position
  - environment is still -> a force (water, fire, wind) SURGES then settles
A clip that is "fast all the way through" or "slow all the way through" has no peak.
Build a trajectory the eye can follow toward a single moment of release.

FRAME RULE: No close-up face shots. No direct action on a character's body.
For close shots use hands, feet, objects, textures, materials.

Rules:
- 20-35 words per prompt -- specific noun + verb + environment, no filler adjectives
- Story progresses: arrival -> challenge -> peak -> resolution
- Return ONLY valid JSON: {"clips": ["prompt1", "prompt2", ...]}\
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
    mood   = analysis.get("mood", "cinematic")

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
        f"Song character: {song_desc or 'cinematic'}\n"
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
            result = [str(c) for c in clips[:n_clips]]
            # If LLM returned fewer clips than requested (truncated response),
            # cycle through what we have rather than repeating the last prompt --
            # repeating causes visually identical consecutive clips.
            src = len(result)
            while len(result) < n_clips:
                result.append(result[len(result) % src])
            log.info("[song-video] Story arc: %d prompts from LLM, padded to %d", src, n_clips)
            return result
    except Exception as e:
        log.warning("[song-video] Story arc LLM call failed: %s", e)

    base = user_idea or "Wide shot of open landscape, camera dollies forward slowly as subject moves toward the horizon, warm late-afternoon light"
    return [base] * n_clips


def _merge_video_audio_trim(
    video_path: str,
    audio_path: str,
    out_path: str,
    audio_duration: float,
) -> str | None:
    """Merge video + audio, looping the video if needed to fill the full song duration."""
    video_dur = probe_duration(video_path) or 0.0
    need_loop = video_dur > 0 and video_dur < audio_duration * 0.98

    if need_loop:
        # Loop the video to fill the song. Re-encode to keep timestamps clean.
        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", video_path,
            "-i", audio_path,
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{audio_duration:.3f}",
            "-movflags", "+faststart",
            out_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{audio_duration:.3f}",
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

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut_beats  = pool.submit(compute_clip_plan, audio_path, n_clips, clip_dur, 19.0)
        fut_lyrics = pool.submit(_transcribe_lyrics, audio_path) if (not lyrics_text and os.path.isfile(audio_path)) else None

        clip_durations, beat_positions = fut_beats.result()
        if fut_lyrics is not None:
            detected = fut_lyrics.result()
            if detected:
                lyrics_text = detected
                log.info("[song-video] Auto-detected %d chars of lyrics", len(lyrics_text))

    # Guard: if the song is shorter than n_clips * min_dur, _place_boundaries
    # collapses trailing boundaries to total_dur, producing zero-duration clips.
    # ffmpeg -t 0 then writes an empty file and all subsequent clips fail.
    clip_durations = [max(8.0, d) for d in clip_durations]
    settings["_clip_durations"] = clip_durations
    settings["_beat_positions"] = beat_positions
    log.info("[song-video] Clip durations (beat-aligned): %s", clip_durations)
    log.info("[song-video] Beat positions per clip: %s", beat_positions)

    job.meta["stage"] = "planning"
    job.update(progress=4, message="Planning music video story arc...")
    try:
        arc = _generate_song_arc(llm_router, n_clips, analysis, user_idea, photo_path, variety_theme, lyrics_text)
        settings["_story_arc"] = arc
        log.info("[song-video] Story arc (%d clips) generated", n_clips)
    except Exception as e:
        log.warning("[song-video] Story arc failed: %s", e)
        settings["_story_arc"] = [user_idea or "Subject erupts into motion"] * n_clips

    job.meta["stage"] = "waiting-gpu"
    job.update(progress=10, message="Story arc ready, waiting for GPU...")


# -- GPU phase -----------------------------------------------------------------

def run_song_pipeline(job, photo_path, settings):
    """Song-video GPU pipeline: N chained clips -> concat -> merge with user's audio."""
    from app import gallery_push
    from features.fun_videos import video_generator
    from services.forge_client import unload_checkpoint, reload_checkpoint

    def _log(msg):
        log.info(msg)
        display = msg.removeprefix("[info] ").removeprefix("[error] ").removeprefix("[warning] ").removeprefix("[success] ")
        job.update(message=display)

    def _stopped():
        return job.stop_event.is_set()

    # -- Settings ----------------------------------------------------------
    n_clips        = int(settings.get("num_clips", 10))
    clip_dur       = float(settings.get("clip_duration", 8.0))
    clip_durations = settings.pop("_clip_durations", None) or [clip_dur] * n_clips
    beat_positions = settings.pop("_beat_positions", None) or [0.5] * n_clips
    model_name     = settings.get("model_name", "LTX-2 Dev19B Distilled")
    resolution    = settings.get("resolution", "580p")
    ow            = settings.get("override_width")
    oh            = settings.get("override_height")
    steps         = int(settings.get("video_steps", 30))
    guidance      = float(settings.get("video_guidance", 7.5))
    seed          = int(settings.get("video_seed", -1))
    audio_path    = settings.get("audio_path", "")   # user's uploaded song
    audio_dur     = float(settings.get("audio_duration", 0.0))
    story_arc     = settings.pop("_story_arc", [])

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

    # -- Free VRAM ---------------------------------------------------------
    forge_unloaded = unload_checkpoint()

    # -- Phase 1: Generate clips -------------------------------------------
    clip_paths: list[str] = []

    prepped_photo: str | None = None
    if photo_path and os.path.isfile(photo_path):
        prepped_photo = _prep_photo(photo_path, tw, th, job_dir)

    _last_error: list[str | None] = [None]
    _chain_frame: str | None = None   # last frame of previous clip -> first frame of next
    _clip_secs: list[float] = []      # per-clip wall-clock times for ETA

    # Cumulative start times so each clip gets its corresponding audio segment
    _clip_start_times: list[float] = []
    _t = 0.0
    for _d in clip_durations:
        _clip_start_times.append(_t)
        _t += _d

    for i, clip_prompt in enumerate(story_arc):
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

        # Clip 0: use the user's uploaded photo as visual anchor.
        # Clips 1+: use the in-motion boundary frame extracted from the trimmed
        # end of the previous clip. Falls back to T2V if extraction failed.
        if i == 0:
            clip_start_image = prepped_photo
            prompt_to_use = clip_prompt
        else:
            clip_start_image = _chain_frame
            # Prefix chain prompts with a scene-lock instruction so the text
            # reinforces visual continuity alongside the start-image conditioning.
            if clip_start_image:
                prompt_to_use = "Continue same scene and subject. " + clip_prompt
            else:
                prompt_to_use = clip_prompt
        finalized = _finalize_prompt(prompt_to_use, model_name)
        clip_out  = str(job_dir / f"clip_{i:02d}_{job.id[:6]}.mp4")
        this_dur  = clip_durations[i] if i < len(clip_durations) else clip_dur

        # Back off guidance for chain clips so the start-image has dominant
        # weight. At 7.5 the text overrides the start frame and characters
        # change scene. At 3.5 the start frame anchors visual content while
        # the text guides camera motion only.
        effective_guidance = guidance if (i == 0 or not clip_start_image) else max(3.5, guidance * 0.45)

        # Extract the audio segment for this clip's time window so LTX-2 can
        # condition the video generation directly on the music. WAV avoids MP3
        # seek-boundary artifacts that would give LTX-2 a misaligned audio window.
        clip_audio_seg: str | None = None
        if audio_path and os.path.isfile(audio_path):
            seg_path = str(job_dir / f"audio_seg_{i:02d}.wav")
            seg_start = _clip_start_times[i] if i < len(_clip_start_times) else 0.0
            clip_audio_seg = _extract_audio_segment(audio_path, seg_start, this_dur, seg_path)
            if not clip_audio_seg:
                log.warning("[song-video] Audio segment extraction failed for clip %d -- text-only mode", clip_num)

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
                audio_source=clip_audio_seg,
                stop_check=_stopped,
                log_fn=_log,
                progress_fn=_video_progress,
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
        # The LLM gave us a story arc with a clear visual climax in each clip,
        # but the climax lands wherever WanGP put it -- not on the song's beat.
        # We detect the natural peak via frame differencing then warp the clip
        # so that peak slides onto target_time. Total clip duration is
        # preserved, so downstream concat math doesn't change.
        # Use the actual clip duration after the trim so target_time can't
        # land beyond the real end if the trim couldn't bring duration into spec.
        post_trim_dur = probe_duration(clip_path) or this_dur
        beat_pos      = beat_positions[i] if i < len(beat_positions) else 0.5
        target_time   = beat_pos * post_trim_dur
        ramped_out    = clip_out.replace(".mp4", "_synced.mp4")
        job.update(progress=pct_end, message=f"Clip {clip_num}/{n_clips} -- syncing peak to beat...")
        try:
            applied, info = align_clip_to_beat(
                clip_path, target_time, post_trim_dur, ramped_out,
            )
            if applied and Path(ramped_out).exists():
                # Replace the original with the ramped version. The chain
                # frame we extract below now comes from the ramped clip,
                # so the next clip's start frame still matches what plays.
                os.replace(ramped_out, clip_path)
                _log(
                    f"[info] Clip {clip_num} beat-synced: peak {info['natural_time']:.2f}s "
                    f"-> {info['target_time']:.2f}s (conf {info['confidence']:.2f})"
                )
            else:
                # Clean up partial output if the ramp aborted mid-write.
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
        # Trim intermediate clips before chain-frame extraction so the shared
        # boundary frame is in-motion, not a fade-out still. Without this trim
        # the hard cut looks like fade-out/fade-in: clip N fades to near-still,
        # clip N+1 starts from that same near-still and slowly wakes up.
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

        # Extract the in-motion boundary frame (now the last frame of the
        # trimmed clip) and feed it as the start image for the next clip.
        if i < n_clips - 1:
            frame_path = str(job_dir / f"chain_{i:02d}.jpg")
            _chain_frame = _extract_last_frame(clip_path, frame_path)
            if not _chain_frame:
                log.debug("[song-video] Frame extraction failed for clip %d -- next clip uses T2V", clip_num)

        if _clip_secs and clip_num < n_clips:
            avg = sum(_clip_secs) / len(_clip_secs)
            rem = (n_clips - clip_num) * avg
            _log(f"[info] Clip {clip_num}/{n_clips} complete -- ~{int(rem // 60)}m {int(rem % 60):02d}s remaining")
        else:
            _log(f"[info] Clip {clip_num}/{n_clips} complete")

    if _stopped():
        if forge_unloaded:
            reload_checkpoint()
        return

    if not clip_paths:
        if forge_unloaded:
            reload_checkpoint()
        raw = _last_error[0] or "No clips generated -- check WanGP is running"
        raise RuntimeError(f"Song video failed: {raw}")

    if forge_unloaded:
        reload_checkpoint()

    job.meta["stage"] = "concatenating"
    job.update(progress=79, message=f"Concatenating {len(clip_paths)} clips...")
    job.meta["clips_generated"] = len(clip_paths)

    # -- Phase 2: Hard-cut concat ---------------------------------------------
    # No dissolve -- clips chain via the identical last/first frame.
    concat_path = str(job_dir / f"concat_{job.id[:6]}.mp4")
    if not _concat_clips(clip_paths, concat_path):
        concat_path = clip_paths[0]

    # -- Phase 3: Merge with user's audio (loop clips to fill song if needed) -
    job.meta["stage"] = "merging"
    job.update(progress=88, message="Looping clips to fill song duration...")

    model_tag  = model_name.split()[0].lower()
    final_path = str(job_dir / f"songvid_{model_tag}_{time.strftime('%H%M%S')}.mp4")

    # Trim to exact song duration so the video doesn't overhang
    effective_dur = audio_dur if audio_dur > 0 else (probe_duration(audio_path) or 0.0)
    if effective_dur <= 0:
        raise RuntimeError("Cannot determine audio duration -- file may be missing or corrupt")
    merged = _merge_video_audio_trim(concat_path, audio_path, final_path, effective_dur)

    if merged:
        job.output = merged
        from core.inbox import copy_to_inbox; copy_to_inbox(job.output)
        job.meta.update({"final_path": merged, "audio_path": audio_path})
        job.message = f"Music video complete! ({len(clip_paths)} clips)"

        try:
            norm = merged.replace("\\", "/")
            idx  = norm.lower().find("/output/")
            url  = norm[idx:] if idx != -1 else f"/output/{Path(merged).name}"
            gallery_push(
                url, tab="song-video",
                prompt=story_arc[0][:120] if story_arc else "",
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

        # Clean up intermediates
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
        # Remove per-clip audio segment WAVs (conditioning inputs, not output audio)
        for seg in job_dir.glob("audio_seg_*.wav"):
            try:
                seg.unlink()
            except Exception:
                pass
    else:
        # Merge failed -- return the concat without audio
        job.output = concat_path
        from core.inbox import copy_to_inbox; copy_to_inbox(job.output)
        job.message = f"Music video done ({len(clip_paths)} clips -- audio merge failed)"
        try:
            from core.session import get_current as get_session
            get_session().add_file(Path(concat_path).name, "video", "song_video", path=concat_path)
        except Exception:
            pass
        for seg in job_dir.glob("audio_seg_*.wav"):
            try:
                seg.unlink()
            except Exception:
                pass
