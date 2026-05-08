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

from core.ffmpeg_utils import probe_duration, extract_last_frame_to_file, extract_first_frame_to_file
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
You are a kinetic action director planning a multi-clip short film from a still photograph.
Generate sequential motion prompts -- each describes 8-12 seconds of explosive physical action.
Each clip starts from the last frame of the previous, so prompts must chain visually.

STEP 1 -- READ THE PHOTO CAREFULLY. Decide which scene type it is:

  TYPE A -- PEOPLE are the main subject (portrait, figure, face, person, group of people):
    - Identify their specific visual markers: hair color/style, clothing color/type, skin tone
    - EVERY prompt must name these features: "red-haired woman in blue jacket erupts..."
    - Without this the video model generates a different person each clip
    - Animate: face, hair, limbs, clothing, hands -- all moving simultaneously

  TYPE B -- LANDSCAPE / ARCHITECTURE / NO PEOPLE (buildings, nature, seascape, cityscape):
    - DO NOT invent characters, soldiers, people, or figures that are not in the photo
    - Animate what is ALREADY IN THE SCENE: sea waves, clouds, flags, fire, smoke, wind,
      light raking across surfaces, weather rolling in, water surging, trees thrashing
    - The camera can react to these forces but does not invent new subjects
    - Example for a coastal tower: "Storm front slams into the clifftop tower, massive
      waves erupt against the rocks below, spray exploding upward, the red flag above
      tears violently in hurricane-force wind, dark clouds race overhead"

Rules for ALL clips regardless of type:
- Each prompt: 35-55 words, begins with an explosive action verb
- Describe motion that is ALREADY IN THE SCENE -- do not hallucinate new subjects
- NO camera moves as the primary event (camera reacts, never leads)
- BANNED: "establishes", "reveals", "opens on", "we see", "the camera", "slowly", "gently"
- Arc: intense opening -> escalation -> dramatic peak
- Return ONLY valid JSON: {"clips": ["prompt1", "prompt2", ...]}\
"""


def _generate_story_arc(
    llm_router,
    initial_idea: str,
    n_clips: int,
    photo_path: str | None,
    progress_fn=None,
) -> tuple[list[str], str]:
    """Generate N sequential motion prompts that form a narrative arc.

    Returns (prompts, method) where method describes how they were generated
    so the caller can show the user what happened.

    Cascade:
      1. Vision call to Ollama (sees the photo, handles NSFW -- never cloud vision)
      2. Text-only call to any provider (uses sanitizer for cloud, user idea as context)
      3. Built-in fallback phases prefixed with user idea (last resort, always works)
    """
    idea_text = (initial_idea or "").strip() or "Create an exciting action-packed short film"
    user_msg = (
        f"Initial idea: {idea_text}\n"
        f"Number of clips: {n_clips}\n\n"
        f"Generate exactly {n_clips} sequential motion prompts as a story arc.\n\n"
        f"REQUIRED OUTPUT FORMAT -- respond with ONLY this JSON, no other text:\n"
        f'{{"clips": ["prompt 1 here", "prompt 2 here"]}}'
    )

    def _parse_clips(text):
        data = parse_json_response(text)
        if not data:
            return None
        clips = data.get("clips", [])
        if not isinstance(clips, list) or not clips:
            return None
        result = [str(c) for c in clips[:n_clips]]
        src = len(result)
        while len(result) < n_clips:
            result.append(result[len(result) % src])
        return result

    # -- Step 1: vision (Ollama only -- never send user photos to cloud vision) --
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
                tier=TIER_BALANCED, system=_STORY_ARC_SYSTEM, max_tokens=1200,
                force_provider="ollama", format_json=True,
            )
            result = _parse_clips(text)
            if result:
                return result, "vision"
            log.warning("[multi] Story arc vision returned unparseable response -- trying text-only")
        except Exception as e:
            log.warning("[multi] Story arc vision call failed (%s) -- trying text-only", e)

    # -- Step 2: text-only (cloud OK, sanitizer handles NSFW text) --
    if progress_fn:
        progress_fn("Planning story arc (text-only)...")
    try:
        text = llm_router.route(
            [{"role": "user", "content": user_msg}],
            tier=TIER_BALANCED, system=_STORY_ARC_SYSTEM, max_tokens=1200,
        )
        result = _parse_clips(text)
        if result:
            return result, "text"
        log.warning("[multi] Story arc text-only returned unparseable response -- using fallback")
    except Exception as e:
        log.warning("[multi] Story arc text-only call failed (%s) -- using fallback", e)

    # -- Step 3: built-in fallback (always works, preserves user idea) --
    base = (initial_idea.strip() + ", ") if initial_idea else ""
    return [(base + _FALLBACK_PHASES[i % len(_FALLBACK_PHASES)]) for i in range(n_clips)], "fallback"


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

def _concat_with_xfade(clip_paths: list[str], out_path: str, fade_dur: float = 1.0) -> bool:
    """Concatenate clips with crossfade transitions via ffmpeg xfade filter.

    xfade offset for clip i = sum(dur[0..i-1]) - i * fade_dur.
    Falls back to hard-cut concat if probing or encoding fails.
    """
    if not clip_paths:
        return False
    if len(clip_paths) == 1:
        shutil.copy2(clip_paths[0], out_path)
        return Path(out_path).exists()

    durations = []
    for p in clip_paths:
        d = probe_duration(p)
        if not d or d <= fade_dur:
            log.warning("[multi] Cannot probe duration for %s -- hard cut fallback", p)
            return _concat_clips(clip_paths, out_path)
        durations.append(d)

    # Build a chained xfade filter: each pair (prev_out, clip_i) fades at the
    # cumulative timeline offset where clip i should start bleeding in.
    filter_parts = []
    cumulative = 0.0
    prev_label = "[0:v]"
    for i in range(1, len(clip_paths)):
        cumulative += durations[i - 1]
        offset = max(0.1, cumulative - i * fade_dur)
        next_label = f"[{i}:v]"
        out_label = "[v]" if i == len(clip_paths) - 1 else f"[xf{i}]"
        filter_parts.append(
            f"{prev_label}{next_label}"
            f"xfade=transition=fade:duration={fade_dur:.3f}:offset={offset:.4f}"
            f"{out_label}"
        )
        prev_label = out_label

    cmd = ["ffmpeg", "-y"]
    for p in clip_paths:
        cmd += ["-i", p]
    cmd += [
        "-filter_complex", ";".join(filter_parts),
        "-map", "[v]", "-an",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        out_path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600)
        if r.returncode == 0 and Path(out_path).exists():
            return True
        log.error("[multi] xfade failed:\n%s", r.stderr.decode(errors="replace")[-1500:])
    except Exception as e:
        log.error("[multi] xfade exception: %s", e)
    return _concat_clips(clip_paths, out_path)


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
    arc, arc_method = _generate_story_arc(
        llm_router, user_idea, n_clips, photo_path,
        progress_fn=lambda msg: job.update(message=msg),
    )
    settings["_story_arc"] = arc
    if arc_method == "vision":
        log.info("[multi] Story arc via vision (%d clips): %s", n_clips, [a[:40] for a in arc])
    elif arc_method == "text":
        log.info("[multi] Story arc via text-only (%d clips): %s", n_clips, [a[:40] for a in arc])
        job.update(message="Story arc planned (photo analysis unavailable, used text)")
    else:
        log.warning("[multi] Story arc using built-in fallback: %s", [a[:40] for a in arc])
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
    # LTX-2 Distilled denoising schedule is compressed into 4-8 steps -- cap to
    # prevent overshooting the schedule (bad output + unnecessary slowness).
    if "ltx" in model_name.lower() and "distilled" in model_name.lower():
        steps    = min(steps, 8)
        guidance = min(guidance, 4.0)
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

        effective_guidance = guidance

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

    job.update(progress=76, message=f"All {len(clip_paths)} clips done -- generating transitions...")
    job.meta["clips_generated"] = len(clip_paths)
    job.meta["clip_paths"] = clip_paths

    # ── Phase 2a: Generate AI bridge clips between adjacent story clips ────
    # Each bridge is a short WanGP clip with start=last_frame_i and
    # end=first_frame_{i+1}, constraining both ends so the transition is
    # visually locked rather than a hard cut or a blind xfade.
    bridge_clips: list[str | None] = []
    if len(clip_paths) > 1 and not _stopped():
        from features.video_bridges.bridge_generator import generate_bridge
        n_bridges = len(clip_paths) - 1
        for i in range(n_bridges):
            if _stopped():
                bridge_clips.append(None)
                continue

            pct = 76 + int((i / n_bridges) * 8)  # 76..84%
            job.update(progress=pct,
                       message=f"Transition {i + 1}/{n_bridges}: morphing clip {i + 1} -> {i + 2}...")

            frame_a = str(job_dir / f"frame_{i:02d}.jpg")          # last frame of clip_i
            frame_b = str(job_dir / f"bfirst_{i + 1:02d}.jpg")     # first frame of clip_{i+1}

            # Extract first frame of the next clip
            got_b = (
                os.path.isfile(frame_a) and
                extract_first_frame_to_file(clip_paths[i + 1], frame_b)
            )
            if not got_b:
                log.warning("[multi] Could not extract boundary frames for bridge %d -- skipping", i)
                bridge_clips.append(None)
                continue

            bridge_out = str(job_dir / f"bridge_{i:02d}.mp4")
            # Build a content-aware bridge prompt so the 2-second clip
            # looks like part of the story rather than a generic filler.
            words_a = " ".join(story_arc[i].split()[:7]) if i < len(story_arc) else ""
            words_b = " ".join(story_arc[i + 1].split()[:7]) if i + 1 < len(story_arc) else ""
            bridge_desc = f"{words_a} transitioning into {words_b}" if words_a and words_b else "fluid cinematic transition"
            bridge_path = generate_bridge(
                frame_a_path=frame_a,
                frame_b_path=frame_b,
                prompt=f"smooth seamless transition, {bridge_desc}, natural motion flow",
                out_path=bridge_out,
                duration=2.0,
                model_name=model_name,
                resolution=resolution,
                steps=_bridge_steps(steps, model_name),
                guidance=guidance,
                seed=seed,
                use_end_frame=True,
                allow_fallback=True,
                stop_check=_stopped,
                log_fn=_log,
            )
            bridge_clips.append(bridge_path if bridge_path and os.path.isfile(bridge_path) else None)
            log.info("[multi] Bridge %d/%d: %s", i + 1, n_bridges,
                     "ok" if bridge_clips[-1] else "fallback/skipped")

    # ── Phase 2b: Compile clips + bridges into one video ─────────────────
    job.update(progress=85, message="Compiling clips with transitions...")
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
        if not _concat_with_xfade(clip_paths, concat_path, fade_dur=1.0):
            log.warning("[multi] Concat failed -- using first clip only")
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
                url, tab="create-videos",
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
