"""Song Video pipeline: audio file → N chained video clips → merge with original audio.

Energy-aware: the LLM reads per-clip energy levels from the audio analysis
and writes motion prompts that match the song's dynamics — explosive action
on loud sections, graceful motion on quiet ones.

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

log = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"


# Hard cut — 0 dissolve. Clips chain via the actual last frame so the content
# at the boundary is identical; a dissolve would make the seam more visible,
# not less. Beat-flash post-processing syncs visual events to audio peaks.
_XFADE_DUR = 0.0


def _extract_last_frame(video_path: str, out_path: str) -> str | None:
    """Extract the actual last frame of a video as a JPEG.

    Probes the duration first then seeks to 2 frames before the end, so the
    extracted frame is the true final frame rather than an arbitrary point
    0.5s before end. This matters for seamless hard-cut chaining — clip N+1
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


def _concat_clips_xfade(clip_paths: list[str], out_path: str, fade_dur: float = _XFADE_DUR) -> bool:
    """Concatenate clips with short xfade dissolves between each pair.

    Each input stream is normalised to 24 fps before the xfade chain so that
    any framerate mismatch between WanGP clips can't produce arithmetic glitches
    in the offset calculation or visual strobing during the blend.

    Falls back to plain concat on any ffmpeg error so the pipeline never stalls.
    """
    if len(clip_paths) == 1:
        shutil.copy2(clip_paths[0], out_path)
        return True

    # Probe each clip duration — needed to compute xfade offsets.
    # Guard: clip must be at least 4× the fade so the overlap never eats into
    # a meaningful portion of a short clip.
    min_dur = fade_dur * 4
    durations: list[float] = []
    for p in clip_paths:
        d = probe_duration(p)
        durations.append(d if d and d > min_dur else max(min_dur + 0.1, 8.0))

    # Normalise each stream to 24 fps + square pixels before xfade.
    norm_parts: list[str] = []
    norm_labels: list[str] = []
    for i in range(len(clip_paths)):
        lbl = f"[n{i}]"
        norm_parts.append(f"[{i}:v]fps=fps=24,setsar=1{lbl}")
        norm_labels.append(lbl)

    # Chain xfade filters.
    # offset = cumulative sum of (dur[i] - fade_dur): the point (in the output
    # timeline) where the next clip's blend begins.
    xfade_parts: list[str] = []
    offset = 0.0
    prev = norm_labels[0]
    for i in range(1, len(clip_paths)):
        offset += durations[i - 1] - fade_dur
        cur   = norm_labels[i]
        label = f"[xf{i}]" if i < len(clip_paths) - 1 else "[vout]"
        xfade_parts.append(
            f"{prev}{cur}xfade=transition=fade"
            f":duration={fade_dur:.3f}:offset={offset:.3f}{label}"
        )
        prev = label

    filter_complex = "; ".join(norm_parts + xfade_parts)

    cmd = (
        ["ffmpeg", "-y"]
        + [arg for p in clip_paths for arg in ["-i", p]]
        + [
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-an",
            out_path,
        ]
    )

    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600)
        if r.returncode == 0 and Path(out_path).exists():
            return True
        log.warning(
            "[song-video] xfade concat failed (rc=%d), falling back to plain concat:\n%s",
            r.returncode,
            r.stderr.decode(errors="replace")[-2000:],
        )
    except Exception as e:
        log.warning("[song-video] xfade concat exception: %s — falling back to plain concat", e)

    return _concat_clips(clip_paths, out_path)


# ── Energy-aware story arc ────────────────────────────────────────────────────

_SONG_ARC_SYSTEM = """\
You write motion prompts for an image-to-video AI generating a music video.
Each prompt is one 8-20 second clip. All clips together tell ONE coherent story.

STORY RULE: Every clip follows the SAME character or subject in the SAME story world
established in Clip 01. The story PROGRESSES — each clip is a different MOMENT or
PHASE of the journey, not a frozen repeat of the same scene.
Example story arc: lone figure at mountain base → climbing through forest →
breaking into open alpine ridge → summit in blazing wind → descent at dusk →
arriving home, transformed.

CAMERA MOTION RULE — every clip must have a purposeful camera MOVE that creates
visual dynamism. Pick one per clip and describe it explicitly:
  zoom out (pull back to reveal), zoom in (push toward subject), slow pan left/right,
  dolly forward (glide through environment), tilt up (sweep from ground to sky),
  orbit/arc (camera circles the subject), crane up, drift (slow float).
Camera moves should feel MOTIVATED by the story — a zoom-out after a summit reveals
the scale; a dolly-forward into darkness builds tension. Do NOT describe static shots.
Each move naturally evolves FROM the previous clip's final frame since clips are chained.

STYLE RULE: Establish a specific color palette and look in Clip 01 (e.g. "cinematic
wide shot, warm amber and deep shadow, golden-hour light"). Carry that COLOR
TEMPERATURE and MOOD through every clip — but the composition and framing MUST
change each clip.

FRAME RULE: No close-up face shots. No direct action on a character's body.
For close shots use hands, feet, objects, texture. The renderer distorts faces.

ENERGY RULE — energy label sets motion speed and intensity:
  HIGH → violent scene motion: rushing water, fire surge, wind blast, fast travel,
         crashing, swirling storm, rapid movement through environment
  MED  → dynamic flow: fabric in wind, light sweeping landscape, steady travel,
         rippling water, drifting smoke, rhythmic natural motion
  LOW  → held, breathing: mist settling, single candle, still water, slow dawn,
         a moment of stillness before or after action

BEAT TIMING RULE — the beat position tells you WHEN peak motion should occur:
  EARLY beat (0-33%): motion erupts from frame one — sudden burst, instant velocity,
                      explosive action from the very start, full intensity immediately
  MID beat (34-66%): steady build to a central PEAK — accelerate into the midpoint,
                     reach maximum intensity at center, then sustain or release
  LATE beat (67-100%): long mounting tension — motion builds the full clip length,
                       erupts or crashes in the final seconds
Write your motion arc so the visual climax lands ON the beat position. Use words
like "suddenly erupts", "builds toward a shattering peak", "culminates in", or
"explodes open" to signal WHERE the intensity peaks.

Rules:
- 30-50 words per prompt — include shot type, subject action, and environment
- Story progresses across clips: arrival → challenge → peak → resolution
- Return ONLY valid JSON: {"clips": ["prompt1", "prompt2", ...]}\
"""


def _beat_timing_label(pos: float) -> str:
    if pos <= 0.33:
        return f"EARLY beat ({int(pos * 100)}%) — open with impact"
    if pos <= 0.66:
        return f"MID beat ({int(pos * 100)}%) — build to center peak"
    return f"LATE beat ({int(pos * 100)}%) — build throughout, erupt at end"


def _generate_song_arc(
    llm_router,
    n_clips: int,
    analysis: dict,
    user_idea: str,
    photo_path: str | None,
    variety_theme: str = "",
    lyrics_text: str = "",
    beat_positions: list[float] | None = None,
) -> list[str]:
    """Generate N motion prompts calibrated to the song's energy profile."""
    energy_profile = analysis.get("energy_profile", [])
    clip_labels    = analysis.get("clip_energy_labels", [])
    bpm    = analysis.get("bpm")
    key    = analysis.get("key", "")
    mode   = analysis.get("mode", "")
    mood   = analysis.get("mood", "cinematic")

    # Build per-clip energy + beat timing hint lines
    clip_hints = []
    for i in range(n_clips):
        if i < len(clip_labels):
            label = clip_labels[i]
            pct   = int((energy_profile[i] if i < len(energy_profile) else 0.5) * 100)
        else:
            label, pct = "MED", 50
        bp = beat_positions[i] if beat_positions and i < len(beat_positions) else 0.5
        clip_hints.append(f"Clip {i + 1:02d}: {label} ({pct}%), {_beat_timing_label(bp)}")

    energy_text = "\n".join(clip_hints)
    key_str = f"{key} {mode}".strip() if key else ""
    bpm_str = f"{bpm} BPM" if bpm else ""
    song_desc = ", ".join(filter(None, [key_str, bpm_str, mood]))

    story_direction = (user_idea or "").strip() or "a compelling cinematic music video"
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
        f"\nEnergy level per clip ({n_clips} clips):\n{energy_text}\n\n"
        f"Generate exactly {n_clips} motion prompts that continue the SAME story "
        f"and match these energy levels."
    )

    try:
        frames = []
        if photo_path and os.path.isfile(photo_path):
            b64 = encode_image_b64(photo_path)
            if b64:
                frames = [b64]
        # Budget ~150 tokens per clip (50-word prompt ≈ 70 tokens + JSON overhead).
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
            log.warning("[song-video] Story arc: LLM returned no parseable JSON — raw: %.200s", text)
            raise ValueError("No JSON in LLM response")
        clips = data.get("clips", [])
        if isinstance(clips, list) and clips:
            result = [str(c) for c in clips[:n_clips]]
            # If LLM returned fewer clips than requested (truncated response),
            # cycle through what we have rather than repeating the last prompt —
            # repeating causes visually identical consecutive clips.
            src = len(result)
            while len(result) < n_clips:
                result.append(result[len(result) % src])
            log.info("[song-video] Story arc: %d prompts from LLM, padded to %d", src, n_clips)
            return result
    except Exception as e:
        log.warning("[song-video] Story arc LLM call failed: %s", e)

    base = user_idea or "Sweeping landscape at golden hour, light surges across the horizon, colours bloom and shift with the energy of the music, wide cinematic shot"
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


# ── Prep phase ────────────────────────────────────────────────────────────────

def run_song_prep(job, photo_path, settings):
    """Phase 0: energy-aware story arc — LLM only, no GPU."""
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

    # Run beat alignment + lyric detection concurrently — both CPU, no GPU needed.
    job.update(progress=2, message="Analysing beat structure and detecting lyrics…")
    from features.song_video.audio_analyzer import compute_clip_plan, _transcribe_lyrics

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut_beats  = pool.submit(compute_clip_plan, audio_path, n_clips, clip_dur, 19.0, _XFADE_DUR)
        fut_lyrics = pool.submit(_transcribe_lyrics, audio_path) if (not lyrics_text and os.path.isfile(audio_path)) else None

        clip_durations, beat_positions = fut_beats.result()
        if fut_lyrics is not None:
            detected = fut_lyrics.result()
            if detected:
                lyrics_text = detected
                log.info("[song-video] Auto-detected %d chars of lyrics", len(lyrics_text))

    settings["_clip_durations"] = clip_durations
    settings["_beat_positions"] = beat_positions
    log.info("[song-video] Clip durations (beat-aligned, xfade-compensated): %s", clip_durations)
    log.info("[song-video] Beat positions per clip: %s", beat_positions)

    job.update(progress=4, message="Planning music video story arc…")
    try:
        arc = _generate_song_arc(llm_router, n_clips, analysis, user_idea, photo_path, variety_theme, lyrics_text, beat_positions)
        settings["_story_arc"] = arc
        log.info("[song-video] Story arc (%d clips) generated", n_clips)
    except Exception as e:
        log.warning("[song-video] Story arc failed: %s", e)
        settings["_story_arc"] = [user_idea or "Subject erupts into motion"] * n_clips

    job.update(progress=10, message="Story arc ready, waiting for GPU…")


# ── GPU phase ─────────────────────────────────────────────────────────────────

def run_song_pipeline(job, photo_path, settings):
    """Song-video GPU pipeline: N chained clips → concat → merge with user's audio."""
    from app import gallery_push
    from features.fun_videos import video_generator
    from services.forge_client import unload_checkpoint, reload_checkpoint

    def _log(msg):
        log.info(msg)
        display = msg.removeprefix("[info] ").removeprefix("[error] ").removeprefix("[warning] ").removeprefix("[success] ")
        job.update(message=display)

    def _stopped():
        return job.stop_event.is_set()

    # ── Settings ──────────────────────────────────────────────────────────
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
        raise RuntimeError("Audio file not found — please re-upload the song")

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

    # ── Free VRAM ─────────────────────────────────────────────────────────
    forge_unloaded = unload_checkpoint()

    # ── Phase 1: Generate clips ───────────────────────────────────────────
    clip_paths: list[str] = []
    analysis_data  = settings.get("audio_analysis", {})
    energy_labels  = analysis_data.get("clip_energy_labels", [])

    # Energy → direct motion-speed vocabulary for WanGP.
    # Injected after the LLM story prompt so the renderer gets explicit cues
    # regardless of how well the LLM translated the energy label.
    _ENERGY_MOTION = {
        "HIGH": "extremely fast motion, explosive burst of speed, dynamic blur, rapid kinetic energy",
        "MED":  "steady flowing motion, smooth continuous movement, rhythmic energy",
        "LOW":  "slow graceful motion, gentle drift, serene stillness, unhurried pace",
    }

    prepped_photo: str | None = None
    if photo_path and os.path.isfile(photo_path):
        prepped_photo = _prep_photo(photo_path, tw, th, job_dir)

    _last_error: list[str | None] = [None]
    _chain_frame: str | None = None   # last frame of previous clip → first frame of next
    _clip_secs: list[float] = []      # per-clip wall-clock times for ETA

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
            eta_str = f" — ~{int(rem // 60)}m {int(rem % 60):02d}s left"

        job.update(progress=pct_start, message=f"Clip {clip_num}/{n_clips}{eta_str}…")
        _clip_t0 = time.time()

        def _video_progress(step, total, _s=pct_start, _e=pct_end, _cn=clip_num, _et=eta_str):
            pct = _s + int(step / total * (_e - _s)) if total > 0 else _s
            job.update(progress=pct, message=f"Clip {_cn}/{n_clips} — step {step}/{total}{_et}")

        energy_label = energy_labels[i] if i < len(energy_labels) else "MED"
        energy_motion = _ENERGY_MOTION.get(energy_label.upper(), _ENERGY_MOTION["MED"])
        finalized = _finalize_prompt(f"{clip_prompt.rstrip('.,;')}, {energy_motion}", model_name)
        # Clip 0: use the user's uploaded photo as visual anchor.
        # Clips 1+: use the last frame of the previous clip so transitions are
        # seamless. The SHOT RULE forces a different camera angle/framing each
        # clip so the chained start-frame doesn't cause visual freeze.
        if i == 0:
            clip_start_image = prepped_photo
        else:
            clip_start_image = _chain_frame   # None if extraction failed — falls back to T2V
        clip_out         = str(job_dir / f"clip_{i:02d}_{job.id[:6]}.mp4")
        this_dur          = clip_durations[i] if i < len(clip_durations) else clip_dur

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
                guidance=guidance,
                seed=seed,
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
            _log(f"[error] Clip {clip_num} produced no output — stopping early")
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
                log.debug("[song-video] Clip %d trimmed %.2fs → %.2fs", clip_num, actual_dur, this_dur)

        clip_paths.append(clip_path)
        _clip_secs.append(time.time() - _clip_t0)

        # Extract the actual last frame and use it as the start image of the
        # next clip. Hard-cut chaining works because both clips share the
        # identical boundary frame, making the cut invisible when motion is
        # continuous. CAMERA MOTION RULE in the system prompt ensures each clip
        # starts from a natural continuation of that shared frame.
        if i < n_clips - 1:
            frame_path = str(job_dir / f"chain_{i:02d}.jpg")
            _chain_frame = _extract_last_frame(clip_path, frame_path)
            if not _chain_frame:
                log.debug("[song-video] Frame extraction failed for clip %d — next clip uses T2V", clip_num)

        if _clip_secs and clip_num < n_clips:
            avg = sum(_clip_secs) / len(_clip_secs)
            rem = (n_clips - clip_num) * avg
            _log(f"[info] Clip {clip_num}/{n_clips} complete — ~{int(rem // 60)}m {int(rem % 60):02d}s remaining")
        else:
            _log(f"[info] Clip {clip_num}/{n_clips} complete")

    if forge_unloaded:
        reload_checkpoint()

    if _stopped():
        return

    if not clip_paths:
        raw = _last_error[0] or "No clips generated — check WanGP is running"
        raise RuntimeError(f"Song video failed: {raw}")

    job.update(progress=79, message=f"Concatenating {len(clip_paths)} clips…")
    job.meta["clips_generated"] = len(clip_paths)

    # ── Phase 2: Hard-cut concat ─────────────────────────────────────────────
    # No dissolve — clips chain via the identical last/first frame.
    concat_path = str(job_dir / f"concat_{job.id[:6]}.mp4")
    if not _concat_clips(clip_paths, concat_path):
        concat_path = clip_paths[0]

    # ── Phase 3: Merge with user's audio (loop clips to fill song if needed) ─
    job.update(progress=88, message="Looping clips to fill song duration…")

    model_tag  = model_name.split()[0].lower()
    final_path = str(job_dir / f"songvid_{model_tag}_{time.strftime('%H%M%S')}.mp4")

    # Trim to exact song duration so the video doesn't overhang
    effective_dur = audio_dur if audio_dur > 0 else (probe_duration(audio_path) or 0.0)
    if effective_dur <= 0:
        raise RuntimeError("Cannot determine audio duration — file may be missing or corrupt")
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
    else:
        # Merge failed — return the concat without audio
        job.output = concat_path
        from core.inbox import copy_to_inbox; copy_to_inbox(job.output)
        job.message = f"Music video done ({len(clip_paths)} clips — audio merge failed)"
        try:
            from core.session import get_current as get_session
            get_session().add_file(Path(concat_path).name, "video", "song_video", path=concat_path)
        except Exception:
            pass
