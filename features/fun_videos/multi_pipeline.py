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

from core.ffmpeg_utils import probe_duration, extract_last_frame_to_file
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

_STORY_ARC_SYSTEM = """\
You are a visual storytelling director planning a multi-clip short film.
Generate sequential motion prompts -- each describes 8-12 seconds of continuous physical action.
Clips must connect visually: each prompt picks up from where the previous clip ended.

Rules:
- Each prompt: 30-50 words, begins with an explosive action verb
- Describe subject motion only -- no camera moves as the primary event
- Narrative arc: establish scene -> build tension -> climax or resolution
- Return ONLY valid JSON: {"clips": ["prompt1", "prompt2", ...]}\
"""


def _generate_story_arc(
    llm_router,
    initial_idea: str,
    n_clips: int,
    photo_path: str | None,
) -> list[str]:
    """Generate N sequential motion prompts that form a narrative arc."""
    idea_text = (initial_idea or "").strip() or "Create an exciting action-packed short film"
    user_msg = (
        f"Initial idea: {idea_text}\n"
        f"Number of clips: {n_clips}\n\n"
        f"Generate exactly {n_clips} sequential motion prompts as a story arc."
    )
    try:
        frames = []
        if photo_path and os.path.isfile(photo_path):
            b64 = encode_image_b64(photo_path)
            if b64:
                frames = [b64]
        if frames:
            text = llm_router.route_vision(
                user_msg, frames,
                tier=TIER_BALANCED, system=_STORY_ARC_SYSTEM, max_tokens=1200,
            )
        else:
            text = llm_router.route(
                [{"role": "user", "content": user_msg}],
                tier=TIER_BALANCED, system=_STORY_ARC_SYSTEM, max_tokens=1200,
            )
        data = parse_json_response(text)
        if data is None:
            raise ValueError("No JSON in LLM response")
        clips = data.get("clips", [])
        if isinstance(clips, list) and clips:
            result = [str(c) for c in clips[:n_clips]]
            # Pad if LLM returned fewer than requested -- cycle to avoid
            # consecutive visually identical clips from repeating the last prompt
            src = len(result)
            while len(result) < n_clips:
                result.append(result[len(result) % src])
            return result
    except Exception as e:
        log.warning("[multi] Story arc LLM call failed: %s", e)

    base = (initial_idea.strip() + ", ") if initial_idea else ""
    return [(base + _FALLBACK_PHASES[i % len(_FALLBACK_PHASES)]) for i in range(n_clips)]


# ── ffmpeg clip concatenation ─────────────────────────────────────────────────

def _concat_clips(clip_paths: list[str], out_path: str) -> bool:
    """Losslessly concatenate video clips using ffmpeg concat demuxer."""
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
                "-c:v", "copy",
                "-an",      # drop any audio from raw WanGP clips
                out_path,
            ],
            capture_output=True, timeout=300,
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

    n_clips         = int(settings.get("num_clips", 4))
    user_idea       = settings.get("video_prompt", "") or settings.get("user_direction", "")
    skip_audio      = settings.get("skip_audio", False)
    instrumental    = settings.get("instrumental", False)
    lyric_direction = settings.get("lyric_direction", "")

    # ── Story arc ─────────────────────────────────────────────────────────
    job.update(progress=3, message="Planning story arc...")
    try:
        arc = _generate_story_arc(llm_router, user_idea, n_clips, photo_path)
        settings["_story_arc"] = arc
        log.info("[multi] Story arc (%d clips): %s", n_clips, [a[:40] for a in arc])
    except Exception as e:
        log.warning("[multi] Story arc failed: %s", e)
        base = (user_idea.strip() + ", ") if user_idea else ""
        settings["_story_arc"] = [(base + _FALLBACK_PHASES[i % len(_FALLBACK_PHASES)]) for i in range(n_clips)]

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
            video_context = " ".join(arc[:2]) if arc else user_idea
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
    n_clips       = int(settings.get("num_clips", 4))
    clip_dur      = float(settings.get("clip_duration", settings.get("video_duration", 8.0)))
    model_name    = settings.get("model_name", "LTX-2 Dev19B Distilled")
    resolution    = settings.get("resolution", "580p")
    ow            = settings.get("override_width")
    oh            = settings.get("override_height")
    steps         = int(settings.get("video_steps", 30))
    guidance      = float(settings.get("video_guidance", 7.5))
    seed          = int(settings.get("video_seed", -1))
    skip_audio    = settings.get("skip_audio", False)
    instrumental  = settings.get("instrumental", False)
    lyric_dir     = settings.get("lyric_direction", "")
    user_dir      = settings.get("user_direction", "")
    music_prompt  = settings.get("music_prompt", "") or settings.pop("_prepped_music_prompt", "")
    lyrics        = settings.pop("_prepped_lyrics", "")
    story_arc     = settings.pop("_story_arc", [])

    if not story_arc:
        base = (settings.get("video_prompt", "").strip() + ", ") if settings.get("video_prompt") else ""
        story_arc = [(base + _FALLBACK_PHASES[i % len(_FALLBACK_PHASES)]) for i in range(n_clips)]

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
    clip_paths: list[str] = []
    start_image_path: str | None = None   # None → clip 0 uses original photo

    # Pre-process the original photo to exact WanGP dimensions
    prepped_photo: str | None = None
    if photo_path and os.path.isfile(photo_path):
        prepped_photo = _prep_photo(photo_path, tw, th, job_dir)

    _last_error: list[str | None] = [None]

    for i, clip_prompt in enumerate(story_arc):
        if _stopped():
            break

        clip_num = i + 1
        pct_start = 10 + int((i / n_clips) * 65)
        pct_end   = 10 + int(((i + 1) / n_clips) * 65)

        job.update(progress=pct_start, message=f"Generating clip {clip_num} of {n_clips}...")

        def _video_progress(step, total, _s=pct_start, _e=pct_end, _cn=clip_num):
            pct = _s + int(step / total * (_e - _s)) if total > 0 else _s
            job.update(progress=pct, message=f"Clip {_cn}/{n_clips} -- step {step}/{total}")

        finalized = _finalize_prompt(clip_prompt, model_name)
        clip_start_image = start_image_path if start_image_path else prepped_photo
        clip_out = str(job_dir / f"clip_{i:02d}_{job.id[:6]}.mp4")

        # Back off guidance for chain clips so start-image has dominant weight.
        # At full guidance the text overrides the start frame and characters drift.
        # At 0.45x the start frame anchors visual content while text guides motion.
        effective_guidance = guidance if (i == 0 or not clip_start_image) else max(2.0, guidance * 0.7)

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
                guidance=effective_guidance,
                seed=seed,
                stop_check=_stopped,
                log_fn=_log,
                progress_fn=_video_progress,
            )
        except Exception as e:
            err = str(e)
            _log(f"[error] Clip {clip_num} failed: {err}")
            _last_error[0] = err
            # Check for VRAM OOM — restart WanGP and abort
            if "out of memory" in err.lower() or "cuda error" in err.lower():
                from services import manager as _svc
                threading.Thread(target=_svc.restart_service, args=("wangp",), daemon=True).start()
            break

        if not clip_path:
            _log(f"[error] Clip {clip_num} produced no output — stopping early")
            break

        clip_paths.append(clip_path)
        _log(f"[info] Clip {clip_num}/{n_clips} complete")

        # Trim LTX-2 fade-out tail before chain frame extraction so the shared
        # boundary frame is in-motion, not a near-static fade-out frame.
        if i < len(story_arc) - 1:
            clip_real_dur = probe_duration(clip_path) or clip_dur
            trim_to = max(clip_real_dur - 0.2, clip_real_dur * 0.9)
            tail_out = str(job_dir / f"clip_{i:02d}_fe.mp4")
            tr = subprocess.run(
                ["ffmpeg", "-y", "-i", clip_path, "-t", f"{trim_to:.4f}", "-c", "copy", tail_out],
                capture_output=True, timeout=60,
            )
            if tr.returncode == 0 and Path(tail_out).exists():
                os.replace(tail_out, clip_path)
            else:
                log.debug("[multi] Clip %d tail-trim failed -- using full clip", i + 1)

        # Extract last frame for the next clip's start_image
        if i < len(story_arc) - 1:
            frame_out = str(job_dir / f"frame_{i:02d}.jpg")
            if extract_last_frame_to_file(clip_path, frame_out):
                start_image_path = frame_out
                log.debug("[multi] Frame %d extracted -> %s", i + 1, frame_out)
            else:
                log.warning("[multi] Frame extraction failed for clip %d -- next clip starts blind", i + 1)
                start_image_path = None

    if _stopped():
        if forge_unloaded:
            reload_checkpoint()
        return

    if not clip_paths:
        if forge_unloaded:
            reload_checkpoint()
        raw = _last_error[0] or "No clips generated -- check WanGP is running"
        raise RuntimeError(f"Multi-video failed: {raw}")

    job.update(progress=76, message=f"Concatenating {len(clip_paths)} clips...")
    job.meta["clips_generated"] = len(clip_paths)
    job.meta["clip_paths"] = clip_paths

    # ── Phase 2: Concatenate ──────────────────────────────────────────────
    concat_path = str(job_dir / f"concat_{job.id[:6]}.mp4")
    if not _concat_clips(clip_paths, concat_path):
        log.warning("[multi] Concat failed — falling back to first clip")
        concat_path = clip_paths[0]

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
    audio_dur = min(total_dur + 2.0, 300.0) if total_dur > 0 else float(n_clips * clip_dur + 2.0)

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
        job.output = merged
        from core.inbox import copy_to_inbox; copy_to_inbox(job.output)
        job.meta.update({"final_path": merged, "music_prompt": music_prompt})
        job.message = f"Multi-video complete! ({len(clip_paths)} clips)"

        # Gallery push
        try:
            norm = merged.replace("\\", "/")
            idx  = norm.lower().find("/output/")
            url  = norm[idx:] if idx != -1 else f"/output/{Path(merged).name}"
            gallery_push(
                url, tab="fun-videos",
                prompt=story_arc[0][:120] if story_arc else "",
                model=model_name,
                metadata={
                    "path": merged,
                    "job_id": job.id,
                    "clips": len(clip_paths),
                    "story_arc": story_arc,
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
