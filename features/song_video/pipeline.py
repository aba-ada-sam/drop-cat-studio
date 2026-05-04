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

from core.ffmpeg_utils import probe_duration, extract_last_frame_to_file
from core.llm_client import TIER_BALANCED, encode_image_b64, parse_json_response
from features.fun_videos.pipeline import _prep_photo, _finalize_prompt
from features.fun_videos.multi_pipeline import _concat_clips

log = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"


# 0.3 s is the standard "invisible cut" duration in professional editing.
# Each clip already starts from the extracted last frame of the previous one,
# so the two images at the splice point are nearly identical — a 0.3 s blend
# is genuinely imperceptible while still smoothing any motion-direction change.
_XFADE_DUR = 0.3


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
You write motion prompts for an image-to-video AI that will be set to music.
Each prompt describes one 8-20 second clip of continuous physical action.

STORY RULE — CRITICAL: All clips are one unbroken story. The SAME subject and
scene established in Clip 01 continue through every clip. Do NOT introduce new
subjects, locations, or settings mid-story. The viewer must feel they are
watching a single continuous sequence, not a montage of unrelated scenes.

The energy level tells you HOW the subject moves in each clip:
  HIGH energy → explosive verbs: erupts, slams, whips, surges, tears, launches
  MED energy  → dynamic verbs: flows, pulses, swings, arcs, spirals, unfolds
  LOW energy  → graceful verbs: drifts, glides, sways, breathes, melts, settles

Rules:
- 30-50 words per prompt, one tight paragraph, no camera moves as the primary event
- Each clip picks up motion exactly where the previous clip ended
- Narrative arc across all clips: establish → build tension → peak → resolve
- Return ONLY valid JSON: {"clips": ["prompt1", "prompt2", ...]}\
"""


def _generate_song_arc(
    llm_router,
    n_clips: int,
    analysis: dict,
    user_idea: str,
    photo_path: str | None,
    variety_theme: str = "",
) -> list[str]:
    """Generate N motion prompts calibrated to the song's energy profile."""
    energy_profile = analysis.get("energy_profile", [])
    clip_labels    = analysis.get("clip_energy_labels", [])
    bpm    = analysis.get("bpm")
    key    = analysis.get("key", "")
    mode   = analysis.get("mode", "")
    mood   = analysis.get("mood", "cinematic")

    # Build per-clip energy hint lines
    clip_hints = []
    for i in range(n_clips):
        if i < len(clip_labels):
            label = clip_labels[i]
            pct   = int((energy_profile[i] if i < len(energy_profile) else 0.5) * 100)
        else:
            label, pct = "MED", 50
        clip_hints.append(f"Clip {i + 1:02d}: {label} ({pct}%)")

    energy_text = "\n".join(clip_hints)
    key_str = f"{key} {mode}".strip() if key else ""
    bpm_str = f"{bpm} BPM" if bpm else ""
    song_desc = ", ".join(filter(None, [key_str, bpm_str, mood]))

    story_direction = (user_idea or "").strip() or "a compelling cinematic music video"
    style_line = f"Visual style / aesthetic: {variety_theme}\n" if variety_theme else ""

    user_msg = (
        f"Song character: {song_desc or 'cinematic'}\n"
        f"Story direction: {story_direction}\n"
        f"{style_line}"
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
        # max_tokens must cover n_clips × ~50 words each plus JSON overhead.
        # 1500 was too small for >20 clips; 4096 handles up to ~60 clips safely.
        max_tok = max(2048, n_clips * 80)
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
            while len(result) < n_clips:
                result.append(result[-1])
            return result
    except Exception as e:
        log.warning("[song-video] Story arc LLM call failed: %s", e)

    base = user_idea or "Subject erupts into motion, energy bursts through the frame"
    return [base] * n_clips


def _merge_video_audio_trim(
    video_path: str,
    audio_path: str,
    out_path: str,
    audio_duration: float,
) -> str | None:
    """Merge video + audio, trimming both to exactly audio_duration seconds."""
    try:
        r = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", video_path,
                "-i", audio_path,
                "-map", "0:v",
                "-map", "1:a",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-t", f"{audio_duration:.3f}",   # hard trim to song length
                "-movflags", "+faststart",
                out_path,
            ],
            capture_output=True, timeout=300,
        )
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

    n_clips       = int(settings.get("num_clips", 10))
    user_idea     = settings.get("video_prompt", "") or settings.get("user_direction", "")
    variety_theme = settings.get("variety_theme", "")
    analysis      = settings.get("audio_analysis", {})

    job.update(progress=4, message="Planning music video story arc…")
    try:
        arc = _generate_song_arc(llm_router, n_clips, analysis, user_idea, photo_path, variety_theme)
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
    n_clips      = int(settings.get("num_clips", 10))
    clip_dur     = float(settings.get("clip_duration", 8.0))
    model_name   = settings.get("model_name", "LTX-2 Dev19B Distilled")
    resolution   = settings.get("resolution", "580p")
    ow           = settings.get("override_width")
    oh           = settings.get("override_height")
    steps        = int(settings.get("video_steps", 30))
    guidance     = float(settings.get("video_guidance", 7.5))
    seed         = int(settings.get("video_seed", -1))
    audio_path   = settings.get("audio_path", "")   # user's uploaded song
    audio_dur    = float(settings.get("audio_duration", 0.0))
    story_arc    = settings.pop("_story_arc", [])

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
    start_image_path: str | None = None

    prepped_photo: str | None = None
    if photo_path and os.path.isfile(photo_path):
        prepped_photo = _prep_photo(photo_path, tw, th, job_dir)

    _last_error: list[str | None] = [None]

    for i, clip_prompt in enumerate(story_arc):
        if _stopped():
            break

        clip_num  = i + 1
        pct_start = 10 + int((i / n_clips) * 68)
        pct_end   = 10 + int(((i + 1) / n_clips) * 68)

        job.update(progress=pct_start, message=f"Generating clip {clip_num} of {n_clips}…")

        def _video_progress(step, total, _s=pct_start, _e=pct_end, _cn=clip_num):
            pct = _s + int(step / total * (_e - _s)) if total > 0 else _s
            job.update(progress=pct, message=f"Clip {_cn}/{n_clips} — step {step}/{total}")

        finalized         = _finalize_prompt(clip_prompt, model_name)
        clip_start_image  = start_image_path if start_image_path else prepped_photo
        clip_out          = str(job_dir / f"clip_{i:02d}_{job.id[:6]}.mp4")

        try:
            clip_path = video_generator.generate_video(
                image_path=clip_start_image,
                prompt=finalized,
                out_path=clip_out,
                duration=clip_dur,
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

        clip_paths.append(clip_path)
        _log(f"[info] Clip {clip_num}/{n_clips} complete")

        if i < len(story_arc) - 1:
            frame_out = str(job_dir / f"frame_{i:02d}.jpg")
            if extract_last_frame_to_file(clip_path, frame_out):
                start_image_path = frame_out
            else:
                log.warning("[song-video] Frame extraction failed for clip %d", i + 1)
                start_image_path = None

    if forge_unloaded:
        reload_checkpoint()

    if _stopped():
        return

    if not clip_paths:
        raw = _last_error[0] or "No clips generated — check WanGP is running"
        raise RuntimeError(f"Song video failed: {raw}")

    job.update(progress=79, message=f"Concatenating {len(clip_paths)} clips…")
    job.meta["clips_generated"] = len(clip_paths)

    # ── Phase 2: Concatenate ──────────────────────────────────────────────
    concat_path = str(job_dir / f"concat_{job.id[:6]}.mp4")
    if not _concat_clips_xfade(clip_paths, concat_path):
        log.warning("[song-video] Concat failed — using first clip")
        concat_path = clip_paths[0]

    # ── Phase 3: Merge with user's audio ─────────────────────────────────
    job.update(progress=88, message="Merging with your song…")

    model_tag  = model_name.split()[0].lower()
    final_path = str(job_dir / f"songvid_{model_tag}_{time.strftime('%H%M%S')}.mp4")

    # Trim to exact song duration so the video doesn't overhang
    effective_dur = audio_dur if audio_dur > 0 else probe_duration(audio_path)
    merged = _merge_video_audio_trim(concat_path, audio_path, final_path, effective_dur)

    if merged:
        job.output = merged
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
        job.message = f"Music video done ({len(clip_paths)} clips — audio merge failed)"
        try:
            from core.session import get_current as get_session
            get_session().add_file(Path(concat_path).name, "video", "song_video", path=concat_path)
        except Exception:
            pass
