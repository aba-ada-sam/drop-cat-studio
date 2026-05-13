"""Full Create Videos pipeline: photo -> video -> audio -> merge.

Orchestrates the analyzer, video_generator, and audio_generator modules
into a single job that the job manager can execute.
"""
import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image as _Img

from core import config as cfg
from core.ffmpeg_utils import probe_duration, extract_frame_b64, sample_frames_temporal
from core.llm_client import encode_image_b64
from core.wildcards import expand

log = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"


def _sample_music_frames(video_path: str, llm_router) -> list:
    """Sample video frames optimised for music/lyric analysis.

    Cloud vision APIs (Anthropic, OpenAI) handle 8 frames at 256px cleanly.
    Ollama context windows are tight -- cap at 3 frames so we don't blow them.
    256px is plenty for mood, color, and motion analysis; fine detail wastes tokens.
    """
    is_ollama = llm_router._provider() == "ollama"
    n = 3 if is_ollama else 8
    return sample_frames_temporal(video_path, max_frames=n, max_dim=256)

# Quality suffixes appended to every video prompt before sending to WanGP.
# These are model-family specific tags the models were trained on.
_PROMPT_SUFFIXES = {
    # LTX-2 image conditioning is very strong and produces near-static output without
    # explicit motion language. Force movement with every prompt.
    "ltx":           "dynamic physical motion, kinetic energy, subjects actively moving, motion blur on fast elements, high quality",
    # Calm mode: environment-only motion. Subject-motion keywords directly contradict
    # the calm system prompt and cause LTX to animate the subject, triggering ghosting.
    "ltx_calm":      "gentle atmospheric motion, environment in motion, subject completely still, static shot, fixed camera, photorealistic, high quality",
    # Narrative mode: purposeful story-driven action, no kinetic extremes.
    "ltx_narrative": "single purposeful action, narrative motion, story-driven gesture, physically legible, photorealistic, high quality",
    "wan":           "smooth animation, photorealistic, high quality, detailed",
    # Wan + calm: Wan I2V can be run in calm mode for breathing-photograph stories.
    # Without a matching suffix the per-clip prompt picked up "smooth animation" --
    # a kinetic hint that fights the CALM story-arc system prompt. Mirror the LTX
    # calm suffix so subject-still and static-camera are restated every clip.
    "wan_calm":      "subject completely still, environment in gentle motion, static shot, fixed camera, photorealistic, high quality",
    "wan_narrative": "deliberate purposeful motion, meaningful story action, physically legible gesture, photorealistic, high quality",
}


def _finalize_prompt(prompt: str, model_name: str, motion_style: str | None = None) -> str:
    """Append model-appropriate quality suffix to any video prompt."""
    base = (prompt or "").strip().rstrip(".,;")
    if "ltx" in model_name.lower():
        if motion_style == "calm":
            key = "ltx_calm"
        elif motion_style == "narrative":
            key = "ltx_narrative"
        else:
            key = "ltx"
    else:
        if motion_style == "calm":
            key = "wan_calm"
        elif motion_style == "narrative":
            key = "wan_narrative"
        else:
            key = "wan"
    suffix = _PROMPT_SUFFIXES[key]
    return f"{base}, {suffix}" if base else suffix


def _prep_photo(src: str, target_w: int, target_h: int, job_dir: Path) -> str:
    """Center-crop + resize src image to exactly target_w x target_h.

    WanGP's LTX-2 VAE encoder fails or loops at step 0 when the input image
    dimensions differ from the output resolution.  Pre-matching them here
    prevents that failure without any quality loss (WanGP would resize anyway).
    Falls back to the original path if anything goes wrong.
    """
    try:
        img = _Img.open(src).convert("RGB")
        iw, ih = img.size
        if iw == target_w and ih == target_h:
            return src
        # Center-crop to target aspect ratio
        tr = target_w / target_h
        ir = iw / ih
        if abs(ir - tr) > 0.02:
            if ir > tr:            # wider -- trim sides
                nw = int(ih * tr)
                x  = (iw - nw) // 2
                img = img.crop((x, 0, x + nw, ih))
            else:                   # taller -- trim top/bottom
                nh = int(iw / tr)
                y  = (ih - nh) // 2
                img = img.crop((0, y, iw, y + nh))
        img = img.resize((target_w, target_h), _Img.LANCZOS)
        out = job_dir / "input_prep.jpg"
        img.save(str(out), "JPEG", quality=95)
        log.info("Input image resized %dx%d -> %dx%d for WanGP", iw, ih, target_w, target_h)
        return str(out)
    except Exception as e:
        log.warning("Image prep failed, using original: %s", e)
        return src





def run_prep(job, photo_path, settings):
    """Phase 0: AI pre-analysis -- LLM calls only, no GPU.

    Runs outside the GPU queue so it executes concurrently while the GPU
    is busy with a prior job. Results are written into settings so that
    run_pipeline can skip Phase 0 entirely and go straight to WanGP.
    """
    from app import get_llm_router; llm_router = get_llm_router()
    from features.fun_videos import analyzer

    # For continuation mode: extract last frame of the source video so LLM
    # vision sees where the video ended (the chicken, not just the first frame).
    start_video_path = settings.get("start_video_path", "")
    video_mode       = settings.get("video_mode", "continuation")
    if start_video_path and os.path.isfile(start_video_path) and video_mode == "continuation":
        from core.ffmpeg_utils import extract_last_frame_to_file
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp_path = tmp.name
        tmp.close()
        if extract_last_frame_to_file(start_video_path, tmp_path):
            settings["_start_video_last_frame"] = tmp_path
            log.info("[prep] Continuation mode: last frame extracted from %s", start_video_path)

    music_prompt    = settings.get("music_prompt", "")
    lyric_direction = settings.get("lyric_direction", "")
    user_direction  = settings.get("user_direction", "")
    instrumental    = settings.get("instrumental", False)
    skip_audio      = settings.get("skip_audio", False)
    use_mmaudio     = settings.get("audio_provider", "acestep") == "ltx_native"

    needs_audio = not skip_audio and not use_mmaudio

    video_prompt = settings.get("video_prompt", "")

    # Always auto-generate a kinetic video prompt when the user hasn't written one --
    # applies to all modes (audio, video-only, MMAudio). Without this, blank prompts
    # produce frozen/static clips regardless of audio settings.
    if not video_prompt:
        job.update(progress=3, message="Writing motion prompt...")
        try:
            auto_prompt = analyzer.generate_video_prompt_auto(
                llm_router,
                user_direction=user_direction,
                subject_hint="",
            )
            if auto_prompt:
                settings["_prepped_video_prompt"] = auto_prompt
                video_prompt = auto_prompt  # use as context for music direction below
                log.info("[info] Auto video prompt: %s", auto_prompt[:80])
        except Exception as e:
            log.warning("[warning] Auto prompt failed: %s", e)

    if not needs_audio or (music_prompt and instrumental):
        return  # no audio prep needed -- video prompt is already set above

    job.update(progress=5, message="Getting music direction...")
    try:
        # Cloud providers (Anthropic/OpenAI) must not receive NSFW images.
        # Pass frames only when Ollama is active; cloud gets text context instead.
        provider = llm_router._provider()
        if not music_prompt:
            if provider == "ollama" and photo_path and os.path.isfile(photo_path):
                src_b64 = encode_image_b64(photo_path)
                frames = [src_b64] if src_b64 else []
            else:
                frames = []  # cloud path: text-only, no image sent
            music_result = analyzer.generate_music_prompt(
                llm_router, frames, user_direction, video_prompt=video_prompt
            )
            music_prompt = music_result.get("music_prompt", "")
            scene_desc   = music_result.get("reasoning", "")
            if not settings.get("bpm") and music_result.get("bpm"):
                settings["bpm"] = music_result["bpm"]
            log.info("[info] Music direction: %s", music_prompt[:80])
        else:
            scene_desc = ""
        if not instrumental:
            job.update(progress=8, message="Writing lyrics...")
            lyrics = analyzer.generate_lyrics(
                llm_router, [],
                music_prompt, lyric_direction or user_direction,
                scene_description=scene_desc,
            )
            if lyrics:
                log.info("[info] Lyrics generated")
                settings["_prepped_lyrics"] = lyrics
        if music_prompt:
            settings["_prepped_music_prompt"] = music_prompt
    except Exception as e:
        log.warning("[warning] Pre-analysis failed: %s -- will retry during GPU phase", e)

    job.update(progress=9, message="Analysis complete, waiting for GPU...")


def run_pipeline(job, photo_path, settings):
    """Full pipeline worker function for JobManager.

    Settings keys:
        video_prompt, music_prompt, lyrics, user_direction,
        use_wildcards, video_duration, model_name, resolution,
        video_steps, video_guidance, video_seed,
        audio_steps, audio_guidance, instrumental, audio_format,
        bpm, skip_audio, end_photo_path
    """
    from app import get_llm_router, gallery_push; llm_router = get_llm_router()
    from features.fun_videos import analyzer, video_generator, audio_generator

    def _norm_url(p):
        """Convert absolute Windows path to /output/... URL."""
        norm = str(p).replace("\\", "/")
        idx  = norm.lower().find("/output/")
        return norm[idx:] if idx != -1 else f"/output/{Path(p).name}"

    def _gallery(path, extra_meta=None):
        url = _norm_url(path)
        # Map pipeline settings to the keys applySettings in tab-fun-videos.js expects,
        # so Branch & Tweak can replay the exact generation.
        replay_settings = {
            "steps":        settings.get("video_steps"),
            "guidance":     settings.get("video_guidance"),
            "duration_sec": settings.get("video_duration"),
            "model":        settings.get("model_name"),
            "seed":         settings.get("video_seed"),
            "prompt":       settings.get("video_prompt", ""),
            "source_image": photo_path or "",
        }
        replay_settings = {k: v for k, v in replay_settings.items() if v is not None}
        elapsed = (time.time() - job.started_at) if job.started_at else None
        meta = {"path": str(path), "job_id": job.id, "settings": replay_settings,
                "elapsed_seconds": elapsed, **job.meta}
        if extra_meta:
            meta.update(extra_meta)
        gallery_push(url, tab="create-videos",
                     prompt=job.meta.get("prompt", ""),
                     model=job.meta.get("model", ""),
                     metadata=meta)

    # Setup
    ts = time.strftime("%Y-%m-%d")
    if photo_path:
        slug = Path(photo_path).stem[:20].replace(" ", "_")
    else:
        # Text-to-video mode -- slug from prompt
        video_prompt_raw = settings.get("video_prompt", "")
        slug = "t2v_" + "".join(c if c.isalnum() else "_" for c in video_prompt_raw[:16]).strip("_")
    job_dir = OUTPUT_DIR / ts / f"{slug}_{job.id}"
    job_dir.mkdir(parents=True, exist_ok=True)

    video_prompt = settings.get("video_prompt", "") or settings.pop("_prepped_video_prompt", "")
    music_prompt = settings.get("music_prompt", "") or settings.pop("_prepped_music_prompt", "")
    lyric_direction = settings.get("lyric_direction", "")
    use_wildcards = settings.get("use_wildcards", False)
    skip_audio = settings.get("skip_audio", False)
    use_mmaudio_early = settings.get("audio_provider", "acestep") == "ltx_native"
    user_direction = settings.get("user_direction", "")
    instrumental = settings.get("instrumental", False)
    lyrics = settings.pop("_prepped_lyrics", "")

    # Continuation mode: use the last frame of the source video as the
    # effective start image so the AI continues from where the video ended.
    video_mode = settings.get("video_mode", "continuation")
    prepend_original = None
    video_last_frame = settings.get("_start_video_last_frame")
    if video_last_frame and os.path.isfile(video_last_frame) and video_mode == "continuation":
        photo_path = video_last_frame
        prepend_original = settings.get("start_video_path", "")
        if not os.path.isfile(prepend_original or ""):
            prepend_original = None

    _last_error = [None]

    def _log(msg):
        log.info(msg)
        if "[error]" in msg:
            _last_error[0] = msg.replace("[error] ", "")
        display = msg.removeprefix("[info] ").removeprefix("[error] ").removeprefix("[success] ").removeprefix("[warning] ")
        job.update(message=display)

    def _stopped():
        return job.stop_event.is_set()

    # Copy source photo (optional -- None for text-to-video)
    if photo_path:
        src_copy = job_dir / f"source{Path(photo_path).suffix}"
        shutil.copy2(photo_path, src_copy)

    # Expand wildcards
    fs_root = cfg.get("sd_wildcards_dir") or ""
    if use_wildcards and video_prompt:
        video_prompt = expand(video_prompt, fs_root)
    if use_wildcards and music_prompt:
        music_prompt = expand(music_prompt, fs_root)

    # -- Phase 0: Pre-analysis -- ALL Ollama calls happen HERE, before WanGP --
    # Running Ollama concurrently with WanGP causes VRAM thrashing: both models
    # fight for the same GPU memory and denoising steps balloon from 3s to 15s+.
    # By finishing all AI analysis on the SOURCE IMAGE before WanGP starts we
    # give WanGP a completely clear GPU for the entire video generation phase.
    needs_audio = not skip_audio and not use_mmaudio_early
    if needs_audio and (not music_prompt or (not instrumental and not lyrics)):
        job.update(progress=5, message="Analyzing image for music direction...")
        try:
            src_b64 = encode_image_b64(photo_path) if photo_path and os.path.isfile(photo_path) else None
            if src_b64:
                pre_frames = [src_b64]
                scene_desc = ""
                if not music_prompt:
                    music_result = analyzer.generate_music_prompt(llm_router, pre_frames, user_direction)
                    music_prompt = music_result.get("music_prompt", "")
                    scene_desc  = music_result.get("reasoning", "")  # reasoning field describes the scene
                    if not settings.get("bpm") and music_result.get("bpm"):
                        settings["bpm"] = music_result["bpm"]
                    _log(f"[info] Music direction: {music_prompt[:80]}")
                if not instrumental:
                    job.update(progress=7, message="Writing lyrics...")
                    lyrics = analyzer.generate_lyrics(
                        llm_router, [],  # text-only: faster, avoids qwen3-vl thinking mode
                        music_prompt, lyric_direction or user_direction,
                        scene_description=scene_desc,
                    )
                    if lyrics:
                        _log("[info] Lyrics generated")
        except Exception as e:
            _log(f"[warning] Pre-analysis failed: {e} -- will retry after video")

    if _stopped():
        return

    # -- Phase 1: Video Generation ----------------------------------------
    # Free all VRAM before WanGP starts: unload Forge SD AND kill ACE-Step.
    # -- GPU: acquire WanGP exclusively (orchestrator evicts Forge + ACE-Step) -
    from core.gpu_orchestrator import gpu
    gpu.acquire("wangp", reason="single-clip video gen")

    job.update(progress=10, message="Generating video...")

    def _video_progress(step, total_steps):
        pct = 10 + int(step / total_steps * 48) if total_steps > 0 else 10
        job.update(progress=pct, message=f"Generating video... step {step}/{total_steps}")

    _model_name_here = settings.get("model_name", "")
    # Derive motion_style from settings; fall back to model-family default (calm
    # for LTX, dynamic for Wan) so the right prompt suffix is ALWAYS applied.
    # This mirrors the same fallback in multi_pipeline.py line 994.
    _motion_style_here = settings.get("motion_style") or (
        "calm" if "ltx" in _model_name_here.lower() else "dynamic"
    )
    video_prompt = _finalize_prompt(video_prompt, _model_name_here, _motion_style_here)

    ow = settings.get("override_width")
    oh = settings.get("override_height")
    use_mmaudio = settings.get("audio_provider", "acestep") == "ltx_native"

    if photo_path and os.path.isfile(photo_path):
        if ow and oh:
            _tw, _th = int(ow), int(oh)
        else:
            _native = video_generator.MODELS.get(
                settings.get("model_name", "LTX-2 Dev19B Distilled"), {}
            ).get("res") or (1032, 580)
            _tw, _th = _native
        photo_path = _prep_photo(photo_path, _tw, _th, job_dir)

    _mn = settings.get("model_name", "LTX-2 Dev19B Distilled")
    _steps = int(settings.get("video_steps", 30))
    # LTX-2 Distilled: cap at 8 (beyond 8 regresses on compressed schedule).
    # LTX-2 Dev13B: WanGP enforces minimum 20 for ltxv_13B.
    if "ltx" in _mn.lower() and "distilled" in _mn.lower():
        _steps = min(_steps, 8)
    elif "ltx" in _mn.lower():
        _steps = max(_steps, 20)
    video_path = video_generator.generate_video(
        image_path=photo_path,
        prompt=video_prompt,
        out_path=str(job_dir / f"video_{job.id[:8]}.mp4"),
        duration=float(settings.get("video_duration", 14.0)),
        model_name=_mn,
        resolution=settings.get("resolution", "580p"),
        override_width=int(ow) if ow else None,
        override_height=int(oh) if oh else None,
        mmaudio=use_mmaudio,
        steps=_steps,
        guidance=float(settings.get("video_guidance", 7.5)),
        seed=int(settings.get("video_seed", -1)),
        end_image_path=settings.get("end_photo_path"),
        # Continuation mode: WanGP uses the last frame as start_image (image_path above).
        # Don't also pass start_video or it takes precedence over the image in WanGP.
        start_video_path=None if video_mode == "continuation" else settings.get("start_video_path"),
        loras=settings.get("loras", []),
        negative_prompt=video_generator.negative_prompt_for(_mn),
        stop_check=_stopped,
        log_fn=_log,
        progress_fn=_video_progress,
    )

    if _stopped():
        return
    if not video_path:
        raw = _last_error[0] or "WanGP worker not running -- check Settings and start WanGP"
        if "out of memory" in raw.lower() or "cuda error" in raw.lower():
            from services import manager as _svc
            threading.Thread(target=_svc.restart_service, args=("wangp",), daemon=True).start()
            raise RuntimeError(
                "CUDA out of memory -- WanGP is restarting. "
                "Try fewer steps (<=30), shorter duration (<=8s), or a smaller model, then generate again."
            )
        raise RuntimeError(f"Video generation failed: {raw}")

    # Continuation mode: stitch [original video] + [AI clip] into a single output.
    if prepend_original and os.path.isfile(prepend_original) and video_path:
        job.update(progress=59, message="Stitching original video with AI continuation...")
        from features.fun_videos.multi_pipeline import _normalize_video_for_concat, _concat_clips
        _stitch_w = int(ow) if ow else 1032
        _stitch_h = int(oh) if oh else 580
        norm_orig = str(job_dir / "original_normalized.mp4")
        if _normalize_video_for_concat(prepend_original, norm_orig, _stitch_w, _stitch_h):
            stitched = str(job_dir / f"stitched_{job.id[:8]}.mp4")
            if _concat_clips([norm_orig, video_path], stitched):
                log.info("[pipeline] Stitched original + AI continuation -> %s", stitched)
                video_path = stitched
            else:
                log.warning("[pipeline] Stitch concat failed -- using AI-only output")
        else:
            log.warning("[pipeline] Could not normalize original video -- using AI-only output")

    job.update(progress=60, message="Video generated!")
    job.meta["video_path"] = video_path
    job.meta["video_prompt"] = video_prompt

    # Start ACE-Step NOW -- WanGP has released the GPU, so ACE-Step gets full VRAM.
    # Phase 2 (music prompt via Ollama, ~30s) runs while ACE-Step loads.
    if not skip_audio and not use_mmaudio:
        try:
            # GPU handoff: WanGP -> ACE-Step. Orchestrator evicts WanGP.
            from core.gpu_orchestrator import gpu
            gpu.acquire("acestep", reason="audio gen after video")
        except Exception as _e:
            log.debug("ACE-Step acquire skipped: %s", _e)

    # -- Early exit for video-only ----------------------------------------
    if skip_audio:
        job.output = video_path
        from core.inbox import copy_to_inbox; copy_to_inbox(job.output)
        job.message = "Video generated (no audio)"
        _gallery(video_path)
        return

    # -- LTX-2 native audio (MMAudio) -------------------------------------
    # WanGP already embedded audio via MMAudio -- skip ACE-Step entirely.
    if use_mmaudio:
        job.output = video_path
        from core.inbox import copy_to_inbox; copy_to_inbox(job.output)
        job.message = "Video generated with LTX-2 native audio"
        _gallery(video_path)
        try:
            from core.session import get_current as get_session
            get_session().add_file(Path(video_path).name, "video", "fun_videos", path=video_path)
        except Exception as e:
            log.warning("session.add_file failed: %s", e)
        return

    # -- Phase 2: Music Prompt Generation ---------------------------------
    # Skip if pre-analysis (Phase 0) already produced a music prompt.
    if not music_prompt:
        job.update(progress=65, message="Analyzing video for music...")
        try:
            frames = _sample_music_frames(video_path, llm_router)
            if frames:
                music_result = analyzer.generate_music_prompt(llm_router, frames, user_direction)
                music_prompt = music_result.get("music_prompt", "dark cabaret, accordion, upright bass, brushed snare, smoky bistro atmosphere")
                if not settings.get("bpm") and music_result.get("bpm"):
                    settings["bpm"] = music_result["bpm"]
        except Exception as e:
            _log(f"[warning] Music analysis failed: {e}")
        if not music_prompt:
            music_prompt = "dark cabaret, accordion, upright bass, brushed snare, smoky bistro atmosphere"
    else:
        job.update(progress=65, message="Using pre-generated music direction...")

    if use_wildcards and music_prompt:
        music_prompt = expand(music_prompt, fs_root)

    # -- Phase 2b: Auto-generate lyrics if needed --------------------------
    # Skip if pre-analysis (Phase 0) already produced lyrics.
    if not instrumental and not lyrics:
        job.update(progress=68, message="Writing lyrics...")
        try:
            # Reuse a single frame sample for lyrics -- no need to re-sample
            frames = _sample_music_frames(video_path, llm_router)
            if frames:
                lyrics = analyzer.generate_lyrics(llm_router, frames, music_prompt, lyric_direction or user_direction)
                if lyrics:
                    _log("[info] Auto-generated lyrics from video")
        except Exception as e:
            _log(f"[warning] Lyrics generation failed: {e}")

    if not instrumental and not lyrics:
        lyrics = "[verse]\nSomething moves through the frame\nNothing stays the same\n[chorus]\nLife in motion\nSlipping through the frame"
        _log("[info] Using fallback lyrics")

    # -- Phase 3: Audio Generation ----------------------------------------
    job.update(progress=70, message="Generating audio...")
    # Orchestrator already evicted Forge when WanGP was acquired upstream;
    # the subsequent gpu.acquire("acestep") evicted WanGP and brought ACE-Step
    # into VRAM, so the GPU is correctly arranged for audio generation here.
    video_dur = probe_duration(video_path)
    audio_dur = min(video_dur + 2.0, 120.0) if video_dur > 0 else 30.0

    def _audio_progress(elapsed_s):
        job.update(progress=70 + min(14, elapsed_s // 10),
                   message=f"Generating audio... {elapsed_s}s elapsed")

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

    if _stopped():
        return

    if not audio_path:
        _log(f"[warning] Audio failed: {audio_err} -- video saved without audio")
        job.output = video_path
        from core.inbox import copy_to_inbox; copy_to_inbox(job.output)
        job.message = f"Video saved (no audio -- ACE-Step failed: {audio_err})"
        _gallery(video_path)
        return

    job.update(progress=85, message="Audio generated!")
    job.meta["audio_path"] = audio_path
    job.meta["music_prompt"] = music_prompt

    # -- Phase 4: Merge ---------------------------------------------------
    job.update(progress=90, message="Merging video + audio...")

    model_tag = settings.get("model_name", "ltx2").split()[0].lower()
    final_path = str(job_dir / f"fun_{model_tag}_{time.strftime('%H%M%S')}.mp4")

    merged = video_generator.merge_video_audio(video_path, audio_path, final_path, log_fn=_log)

    from core.session import get_current as get_session
    if merged:
        # -- Phase 5: Upscale (optional) ---------------------------------------
        upscale_on     = settings.get("upscale", True)
        upscale_scale  = float(settings.get("upscale_scale", 2.0))
        upscale_method = settings.get("upscale_method", "ffmpeg")
        if upscale_on and not _stopped():
            job.update(progress=93, message="Upscaling video...")
            try:
                from core.upscaler import upscale_video
                up_path = str(job_dir / f"fun_{model_tag}_{time.strftime('%H%M%S')}_up.mp4")
                up_out, up_err = upscale_video(merged, up_path,
                                               scale=upscale_scale, method=upscale_method)
                if up_out:
                    # Remove unscaled merged intermediate
                    if merged != video_path:
                        try: os.remove(merged)
                        except Exception: pass
                    merged = up_out
                else:
                    log.warning("[pipeline] Upscale failed: %s -- using original", up_err)
            except Exception as _ue:
                log.warning("[pipeline] Upscale error: %s -- using original", _ue)

        job.output = merged
        from core.inbox import copy_to_inbox; copy_to_inbox(job.output)
        job.meta["final_path"] = merged
        job.message = "Complete!"
        _gallery(merged, {"music_prompt": music_prompt})
        try:
            get_session().add_file(Path(merged).name, "video", "fun_videos", path=merged)
        except Exception as e:
            log.warning("session.add_file failed: %s", e)
        # Delete the raw WanGP intermediate -- only the merged file is needed
        if video_path and video_path != merged:
            try:
                os.remove(video_path)
                log.debug("Deleted raw intermediate: %s", video_path)
            except Exception as e:
                log.debug("Could not delete raw intermediate: %s", e)
    else:
        job.output = video_path
        from core.inbox import copy_to_inbox; copy_to_inbox(job.output)
        job.message = "Video generated (audio merge failed)"
        _gallery(video_path)
        try:
            get_session().add_file(Path(video_path).name, "video", "fun_videos", path=video_path)
        except Exception as e:
            log.warning("session.add_file failed: %s", e)
