"""Infinite Zoom pipeline: photo/video -> N chained WanGP clips zooming in or out.

Two phases (via submit_with_prep):
  run_zoom_prep   -- LLM plans zoom arc (CPU, no GPU lock)
  run_zoom_pipeline -- WanGP clips + ACE-Step audio (GPU locked)

Anti-enshittification strategy:
  - LLM plans all clip content upfront from the source image before any GPU work
  - Subject anchor included in every clip prompt to prevent identity drift
  - reanchor_every=0 always (continuous chain -- resetting to source kills parallax)
  - PNG chain frames (lossless last-frame extraction between every clip)
  - Wan I2V preferred (25 steps holds camera motion better than LTX 8 steps)
"""
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"

from core.ffmpeg_utils import probe_duration
from core.llm_client import TIER_BALANCED, TIER_FAST, encode_image_b64, parse_json_response
from core.llm_router import LLMRouter
from features.fun_videos.multi_pipeline import _chain_anchor, _CHAIN_TRIM_RATIO

log = logging.getLogger("zoom")

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_ZOOM_OUT_SYSTEM = """\
You are writing prompts for {n} sequential AI video clips that together form
a continuous zoom-out from a still photograph. Each clip starts from the last
frame of the previous -- one unbroken retreating move seen by the audience.

GOAL: Real spatial depth and parallax -- NOT a digital zoom effect. Each clip
must describe the environment physically expanding around the subject as the
camera retreats through space.

RULES:
- Describe what NEW elements physically appear at the frame edges each clip
  (wall, doorframe, street, trees, sky, crowd -- whatever fits the scene).
- Describe the subject becoming smaller within the growing scene.
- Include ambient motion: light shifting, leaves moving, people in background.
- End every prompt with one of: "smooth continuous pullback" / "steady retreat through space".
- Prompts 40-60 words. Do NOT use negative words.
- Do NOT describe zoom percentages or camera instructions -- describe the SCENE.

Return ONLY valid JSON:
{"subject_anchor": "brief one-line subject description", "clips": [{"prompt": "...", "duration": 5}, ...]}
"""

_ZOOM_IN_SYSTEM = """\
You are writing prompts for {n} sequential AI video clips that together form
a continuous zoom-in from a still photograph. Each clip starts from the last
frame of the previous -- one unbroken advancing move.

GOAL: Real spatial depth and parallax -- NOT a digital zoom effect. Each clip
must describe the environment physically contracting as the camera advances
through space toward a specific focal point.

RULES:
- Choose ONE focal target (face, eye, texture, object detail) and advance toward it.
- Describe what leaves the frame edges as the scene narrows (walls, furniture,
  background objects falling out of view).
- Describe increasing detail becoming visible on the focal target itself.
- Include ambient texture: grain, fabric weave, skin pores, material surface.
- End every prompt with: "smooth continuous push forward through space".
- Prompts 40-60 words. Do NOT use negative words.
- Do NOT describe zoom percentages or camera instructions -- describe the SCENE.

Return ONLY valid JSON:
{"subject_anchor": "brief one-line subject description", "focal_target": "specific feature being approached", "clips": [{"prompt": "...", "duration": 5}, ...]}
"""


# ---------------------------------------------------------------------------
# Arc planning
# ---------------------------------------------------------------------------

def _plan_zoom_arc(
    llm_router: LLMRouter,
    photo_path: str,
    direction: str,
    n_clips: int,
    idea: str,
    total_secs: float,
    progress_fn=None,
) -> tuple[list[dict], str, str]:
    """LLM vision call to plan zoom levels.

    Returns (clips, subject_anchor, focal_target).
    clips is list[{prompt, duration}].
    Falls back to a template arc if LLM fails.
    """
    if progress_fn:
        progress_fn("Planning zoom arc from photo...")

    system_tpl = _ZOOM_OUT_SYSTEM if direction == "out" else _ZOOM_IN_SYSTEM
    system = system_tpl.replace("{n}", str(n_clips))

    _clip_dur = total_secs / max(n_clips, 1)  # user's requested duration per clip

    idea_line = f"\nUser's idea: {idea}" if idea else ""
    user_msg = (
        f"Direction: zoom {direction}\n"
        f"Clips: {n_clips}\n"
        f"Seconds per clip: {_clip_dur:.0f}s\n"
        f"Target total duration: {int(total_secs)}s{idea_line}\n\n"
        f"Generate exactly {n_clips} clip prompts."
    )

    frames = []
    if photo_path and os.path.isfile(photo_path):
        b64 = encode_image_b64(photo_path)
        if b64:
            frames = [b64]

    def _parse(text) -> tuple[list[dict], str, str] | None:
        data = parse_json_response(text)
        if not isinstance(data, dict):
            return None
        raw = data.get("clips", [])
        if not isinstance(raw, list) or not raw:
            return None
        clips = []
        for item in raw[:n_clips]:
            if not isinstance(item, dict):
                continue
            prompt = str(item.get("prompt", "")).strip()
            if not prompt:
                continue
            # Always use the user's requested duration -- the LLM's hint is
            # clamped to 4-10s in its system prompt and would override the
            # user's selection (e.g. 15s clips coming out as 5s clips).
            clips.append({"prompt": prompt, "duration": _clip_dur})
        if not clips:
            return None
        # Pad if LLM returned fewer than requested
        while len(clips) < n_clips:
            clips.append(dict(clips[-1]))
        anchor = str(data.get("subject_anchor", "")).strip()
        focal = str(data.get("focal_target", "")).strip()
        return clips, anchor, focal

    # Try vision first, fall back to text-only
    for attempt, use_frames in enumerate([frames, []]):
        try:
            if use_frames:
                text = llm_router.route_vision(
                    user_msg, use_frames,
                    tier=TIER_BALANCED, system=system, max_tokens=2500,
                    format_json=True,
                )
            else:
                text = llm_router.route(
                    [{"role": "user", "content": user_msg}],
                    tier=TIER_BALANCED, system=system, max_tokens=2500,
                )
            result = _parse(text)
            if result:
                log.info("[zoom] Arc planned via %s (%d clips)",
                         "vision" if use_frames else "text", n_clips)
                return result
        except Exception as e:
            log.warning("[zoom] Arc attempt %d failed: %s", attempt + 1, e)

    # Hardcoded fallback
    log.warning("[zoom] LLM failed -- using template arc")
    return _fallback_arc(direction, n_clips, idea, _clip_dur)


def _fallback_arc(direction: str, n_clips: int, idea: str, clip_dur: float = 5.0) -> tuple[list[dict], str, str]:
    idea_prefix = f"{idea}. " if idea else ""
    if direction == "out":
        phrases = [
            "The scene retreats through space, walls and ceiling emerging at frame edges.",
            "The room opens up, doorways and furniture appearing as the pullback continues.",
            "The building exterior emerges, street and sky visible at frame edges.",
            "The street scene widens, neighboring buildings and environment expanding outward.",
            "A full block of environment revealed, depth and parallax filling the frame.",
            "The neighborhood opens outward, rooftops and treetops framing the widening scene.",
            "A broad vista emerges, rich spatial depth stretching to the horizon.",
            "The landscape expands outward, the original scene now part of the wider world.",
            "An aerial-scale view unfolds, environment vast and deep in all directions.",
            "The world opens to its full scale with rich parallax and spatial depth.",
            "Maximum pullback, the environment stretches with full dimensional depth.",
            "The ultimate wide view, a vast world of spatial depth and distance.",
        ]
        verb = "Smooth continuous pullback through real space, full parallax depth."
    else:
        phrases = [
            "Advancing through space, background elements receding at the edges of frame.",
            "Closing in, peripheral details falling away, fine surface coming into view.",
            "The focal surface expands to fill the frame, surrounding context narrowing.",
            "Advancing further, background soft, fine material detail becoming visible.",
            "The focal surface fills the frame, texture and grain emerging with clarity.",
            "Fine material detail dominates, depth and surface relief richly visible.",
            "Intimate proximity, individual texture elements visible across the surface.",
            "Extreme closeness, fine grain and microscopic detail across the surface.",
            "The texture becomes abstract and rich, depth and complexity at macro scale.",
            "Full macro view, the surface an intricate landscape of detail and shadow.",
            "Ultimate close-up, fine structure of the surface fills the entire frame.",
            "Maximum proximity, pure surface texture, abstract and extraordinary.",
        ]
        verb = "Smooth continuous push forward through real space, full parallax depth."

    clips = []
    for i in range(n_clips):
        phrase = phrases[min(i, len(phrases) - 1)]
        clips.append({
            "prompt": f"{idea_prefix}{phrase} {verb}",
            "duration": clip_dur,
        })
    return clips, "", ""


# ---------------------------------------------------------------------------
# Frame extraction from video
# ---------------------------------------------------------------------------

def extract_frame_from_video(video_path: str, out_png: str, position: str = "last") -> bool:
    """Extract a frame from a video file to PNG.

    position: "first" (frame at 0.1s) or "last" (0.5s before EOF).
    Returns True on success.
    """
    try:
        if position == "last":
            cmd = [
                "ffmpeg", "-y", "-sseof", "-0.5", "-i", video_path,
                "-frames:v", "1", "-q:v", "1", out_png,
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-ss", "0.1", "-i", video_path,
                "-frames:v", "1", "-q:v", "1", out_png,
            ]
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        return r.returncode == 0 and os.path.isfile(out_png)
    except Exception as e:
        log.warning("[zoom] Frame extraction failed: %s", e)
        return False



# ---------------------------------------------------------------------------
# Prep phase (no GPU lock)
# ---------------------------------------------------------------------------

def run_zoom_prep(job, source_path: str, settings: dict) -> None:
    """Plan zoom arc via LLM + music direction. Runs outside GPU queue."""
    from app import get_llm_router
    from features.fun_videos import analyzer

    llm_router = get_llm_router()
    direction = settings.get("zoom_direction", "out")
    n_clips = int(settings.get("n_clips", 5))
    clip_dur = float(settings.get("clip_duration", 5.0))
    idea = settings.get("idea", "").strip()
    skip_audio = settings.get("skip_audio", False)

    job.update(progress=2, message="Planning zoom arc...")

    arc, subject_anchor, focal_target = _plan_zoom_arc(
        llm_router, source_path, direction, n_clips, idea,
        total_secs=n_clips * clip_dur,
        progress_fn=lambda m: job.update(message=m),
    )

    settings["_zoom_arc"] = arc
    settings["_zoom_subject_anchor"] = subject_anchor
    settings["_zoom_focal_target"] = focal_target

    if skip_audio:
        job.update(progress=10, message="Arc ready, waiting for GPU...")
        return

    # Music direction
    if not settings.get("music_prompt"):
        job.update(progress=6, message="Getting music direction...")
        try:
            frames = []
            if source_path and os.path.isfile(source_path):
                b64 = encode_image_b64(source_path)
                if b64:
                    frames = [b64]
            arc_context = " ".join(
                c.get("prompt", "")[:60] for c in arc[:2]
            )
            result = analyzer.generate_music_prompt(
                llm_router, frames, idea or arc_context,
                video_prompt=arc_context,
            )
            if result.get("music_prompt"):
                settings["_prepped_music_prompt"] = result["music_prompt"]
                log.info("[zoom] Music direction: %s", result["music_prompt"][:80])
        except Exception as e:
            log.warning("[zoom] Music prep failed: %s", e)

    # Lyrics
    music_prompt = settings.get("music_prompt") or settings.get("_prepped_music_prompt", "")
    if not settings.get("instrumental") and not settings.get("_prepped_lyrics") and music_prompt:
        job.update(progress=8, message="Writing lyrics...")
        try:
            frames = []
            if source_path and os.path.isfile(source_path):
                b64 = encode_image_b64(source_path)
                if b64:
                    frames = [b64]
            lyrics = analyzer.generate_lyrics(llm_router, frames, music_prompt, "")
            if lyrics:
                settings["_prepped_lyrics"] = lyrics
        except Exception as e:
            log.warning("[zoom] Lyrics prep failed: %s", e)

    # Audio-first: generate ACE-Step audio now (before clips) so each clip can
    # be conditioned on its matching audio segment for genuine audio-video sync.
    # Only runs when audio_first=True AND wangp is not already warm (evicting a
    # warm WanGP worker to run ACE-Step then reloading costs 3-8 min).
    audio_first = settings.get("audio_first", False)
    if audio_first and not skip_audio and music_prompt:
        from core.gpu_orchestrator import gpu as _gp
        if _gp.current == "wangp":
            log.info("[zoom] audio_first skipped -- WanGP already warm; using post-clip audio")
            settings["audio_first"] = False
        else:
            try:
                import tempfile as _af_tmp
                from features.fun_videos import audio_generator as _ag
                # Use a dedicated temp dir -- NOT os.path.dirname(source_path) which may
                # be the frame-extraction temp dir that the pipeline will delete early.
                _audio_out_dir = _af_tmp.mkdtemp(prefix="dcs_zoom_af_")
                settings["_audio_out_dir"] = _audio_out_dir
                job.update(progress=9, message="Generating audio for sync...")
                _gp.acquire("acestep", reason="zoom audio-first before clips")
                planned_dur = n_clips * clip_dur
                _ap, _aerr = _ag.generate_audio(
                    prompt=music_prompt,
                    duration=min(planned_dur + 2.0, 300.0),
                    output_dir=_audio_out_dir,
                    audio_format=settings.get("audio_format", "mp3"),
                    steps=int(settings.get("audio_steps", 8)),
                    guidance=float(settings.get("audio_guidance", 7.0)),
                    seed=-1,
                    lyrics=settings.get("_prepped_lyrics", ""),
                    instrumental=settings.get("instrumental", False),
                    stop_event=job.stop_event,
                )
                if _ap:
                    settings["_audio_path"] = _ap
                    log.info("[zoom] Audio-first: %s", _ap)
                else:
                    log.warning("[zoom] Audio-first generation failed: %s -- falling back", _aerr)
                    settings["audio_first"] = False
                    import shutil as _sh
                    _sh.rmtree(_audio_out_dir, ignore_errors=True)
                    settings.pop("_audio_out_dir", None)
            except Exception as _ae:
                log.warning("[zoom] Audio-first failed: %s -- falling back", _ae)
                settings["audio_first"] = False
                import shutil as _sh2
                _audio_out_dir_exc = settings.pop("_audio_out_dir", None)
                if _audio_out_dir_exc:
                    _sh2.rmtree(_audio_out_dir_exc, ignore_errors=True)

    job.update(progress=10, message="Arc ready, waiting for GPU...")


# ---------------------------------------------------------------------------
# Audio slice helper
# ---------------------------------------------------------------------------

def _slice_audio(audio_path: str, start_sec: float, dur_sec: float, out_path: str) -> bool:
    """Extract a segment of audio into out_path. Returns True on success."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(start_sec), "-t", str(dur_sec),
             "-i", audio_path, "-c:a", "copy", out_path],
            capture_output=True, timeout=30,
        )
        if r.returncode != 0:
            log.debug("[zoom] audio slice failed (start=%.1f dur=%.1f): %s",
                      start_sec, dur_sec, r.stderr.decode(errors="replace")[-200:])
        return r.returncode == 0 and os.path.isfile(out_path)
    except Exception as _e:
        log.debug("[zoom] audio slice error: %s", _e)
        return False


# ---------------------------------------------------------------------------
# GPU phase
# ---------------------------------------------------------------------------

def run_zoom_pipeline(job, source_path: str, settings: dict) -> None:
    """Generate N zoom clips -> concat -> audio -> merge."""
    from app import get_llm_router, gallery_push
    from features.fun_videos import video_generator, audio_generator
    from core.inbox import copy_to_inbox

    llm_router = get_llm_router()

    direction = settings.get("zoom_direction", "out")
    n_clips = int(settings.get("n_clips", 5))
    clip_dur = float(settings.get("clip_duration", 5.0))
    model_name = settings.get("model_name", "LTX-2 Dev19B Distilled")
    skip_audio = settings.get("skip_audio", False)
    instrumental = settings.get("instrumental", False)
    audio_first = settings.get("audio_first", False)
    extend_base_path: str | None = settings.get("extend_base_path")
    music_prompt = settings.pop("_prepped_music_prompt", settings.get("music_prompt", ""))
    lyrics = settings.pop("_prepped_lyrics", settings.get("lyrics", ""))
    audio_first_path: str | None = settings.pop("_audio_path", None)
    arc: list[dict] = settings.pop("_zoom_arc", [])
    subject_anchor: str = settings.pop("_zoom_subject_anchor", "")
    focal_target: str = settings.pop("_zoom_focal_target", "")

    # Build fallback arc if prep didn't run
    if not arc:
        arc, subject_anchor, focal_target = _fallback_arc(direction, n_clips, settings.get("idea", ""), clip_dur)

    def _stopped() -> bool:
        return job.stop_event.is_set()

    # Output directory
    ts = time.strftime("%Y%m%d_%H%M%S")
    job_dir = OUTPUT_DIR / time.strftime("%Y-%m-%d") / f"zoom_{direction}_{Path(source_path).stem[:12]}_{ts}"
    job_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(source_path, job_dir / f"source{Path(source_path).suffix}")

    # Clean up frame-extraction temp dir -- safe once source is copied to job_dir.
    _tmp_dir = settings.pop("_tmp_dir", None)
    if _tmp_dir:
        shutil.rmtree(_tmp_dir, ignore_errors=True)

    # Pop audio-first output dir -- always cleaned up via try/finally below.
    _audio_out_dir = settings.pop("_audio_out_dir", None)

    try:
        _run_zoom_body(
            job=job, source_path=source_path, settings=settings,
            direction=direction, n_clips=n_clips, clip_dur=clip_dur,
            model_name=model_name, skip_audio=skip_audio,
            instrumental=instrumental, audio_first=audio_first,
            audio_first_path=audio_first_path,
            extend_base_path=extend_base_path,
            arc=arc, subject_anchor=subject_anchor, focal_target=focal_target,
            music_prompt=music_prompt, lyrics=lyrics,
            ts=ts, job_dir=job_dir, llm_router=llm_router,
            video_generator=video_generator, audio_generator=audio_generator,
            copy_to_inbox=copy_to_inbox, gallery_push=gallery_push,
            stopped_fn=_stopped,
        )
    finally:
        if _audio_out_dir:
            shutil.rmtree(_audio_out_dir, ignore_errors=True)


def _run_zoom_body(
    *, job, source_path, settings, direction, n_clips, clip_dur,
    model_name, skip_audio, instrumental, audio_first,
    audio_first_path, extend_base_path, arc, subject_anchor, focal_target,
    music_prompt, lyrics, ts, job_dir, llm_router,
    video_generator, audio_generator, copy_to_inbox, gallery_push,
    stopped_fn,
):
    """All zoom pipeline work after setup — extracted so try/finally in
    run_zoom_pipeline guarantees _audio_out_dir cleanup at every exit."""
    def _stopped() -> bool:
        return stopped_fn()

    # Prepare WanGP request settings
    wangp_steps = int(settings.get("steps", 25))
    wangp_guidance = float(settings.get("guidance", 5.0))

    def _log(msg: str) -> None:
        log.info(msg)
        display = msg.removeprefix("[info] ").removeprefix("[error] ").removeprefix("[warning] ")
        job.update(message=display)

    # -- GPU acquire -----------------------------------------------------------
    from core.gpu_orchestrator import gpu
    gpu.acquire("wangp", reason=f"zoom-{direction} {n_clips} clips")

    clip_paths: list[str] = []
    clip_durations: list[float] = []
    prev_frame: str | None = source_path

    try:
        _log(f"[info] Generating {n_clips} zoom-{direction} clips...")

        pct_start, pct_end = 12, 72
        pct_per_clip = (pct_end - pct_start) / n_clips

        for i, clip_spec in enumerate(arc[:n_clips]):
            if _stopped():
                return

            prompt = clip_spec.get("prompt", "")
            dur = float(clip_spec.get("duration", clip_dur))
            # Add trim compensation so the trimmed clip is the right length
            gen_dur = dur / _CHAIN_TRIM_RATIO

            pct = int(pct_start + i * pct_per_clip)
            job.update(progress=pct,
                       message=f"Clip {i + 1}/{n_clips} -- zoom {direction}...")
            log.info("[zoom] Clip %d/%d: %s", i + 1, n_clips, prompt[:80])

            clip_out = str(job_dir / f"clip_{i:02d}_{ts[-6:]}.mp4")

            # Resize start image to match generation resolution.
            # zoom_res may be smaller than the model's native res for speed.
            start_img = prev_frame or source_path
            try:
                _zoom_res = settings.get("zoom_res") or video_generator.MODELS.get(model_name, {}).get("res", (1032, 580))
                tw, th = int(_zoom_res[0]), int(_zoom_res[1])
                from features.fun_videos.pipeline import _prep_photo
                prepped = _prep_photo(start_img, tw, th, job_dir)
            except Exception:
                prepped = start_img

            # Slice the pre-generated audio for this clip's time window so LTX-2
            # can condition the video on it (audio_first mode).
            clip_audio_slice: str | None = None
            if audio_first and audio_first_path:
                _audio_start = sum(
                    float(arc[j].get("duration", clip_dur)) for j in range(i)
                )
                _slice_path = str(job_dir / f"audio_slice_{i:02d}.wav")
                if _slice_audio(audio_first_path, _audio_start, gen_dur, _slice_path):
                    clip_audio_slice = _slice_path
                    log.debug("[zoom] Audio slice %d: start=%.1fs dur=%.1fs", i + 1, _audio_start, gen_dur)

            ok = False
            for attempt in range(2):
                if _stopped():
                    return
                try:
                    result = video_generator.generate_video(
                        image_path=prepped,
                        out_path=clip_out,
                        prompt=prompt,
                        model_name=model_name,
                        steps=wangp_steps,
                        guidance=wangp_guidance,
                        duration=gen_dur,
                        seed=-1,
                        negative_prompt=video_generator.negative_prompt_for(model_name, "dynamic"),
                        stop_check=job.stop_event.is_set,
                        audio_source=clip_audio_slice,
                        progress_fn=lambda cur, tot: job.update(
                            progress=pct + int(pct_per_clip * cur / max(tot, 1)),
                            message=f"Clip {i + 1}/{n_clips} step {cur}/{tot}",
                        ),
                    )
                    if result and os.path.isfile(clip_out):
                        ok = True
                        break
                except Exception as e:
                    log.warning("[zoom] Clip %d attempt %d failed: %s", i + 1, attempt + 1, e)
                    if attempt == 0:
                        time.sleep(2)

            if not ok:
                _log(f"[error] Clip {i + 1} failed -- stopping zoom")
                if clip_paths:
                    break
                job.update(status="error", message=f"Clip {i + 1} generation failed")
                return

            # Strip LTX-2's embedded per-clip audio -- it's generated as part of
            # the joint diffusion pass and cannot be disabled, but it's thrown away
            # when ACE-Step generates the final soundtrack. Removing it here keeps
            # the concat lighter and avoids multiple audio tracks in the output.
            try:
                silent_out = clip_out.replace(".mp4", "_s.mp4")
                r_strip = subprocess.run(
                    ["ffmpeg", "-y", "-i", clip_out, "-c:v", "copy", "-an", silent_out],
                    capture_output=True, timeout=30,
                )
                if r_strip.returncode == 0 and os.path.isfile(silent_out):
                    os.replace(silent_out, clip_out)
            except Exception as _se:
                log.debug("[zoom] Audio strip failed for clip %d (non-fatal): %s", i + 1, _se)

            clip_paths.append(clip_out)

            # Extract last frame as next clip's start (lossless chain, no re-anchor)
            frame_out = str(job_dir / f"frame_{i:02d}.png")
            anchor_ok, actual_dur = _chain_anchor(clip_out, frame_out)
            clip_durations.append(actual_dur)
            if anchor_ok:
                prev_frame = frame_out
            else:
                log.warning("[zoom] Chain anchor failed for clip %d -- next uses source", i + 1)
                prev_frame = source_path

        if not clip_paths:
            job.update(status="error", message="No clips generated")
            return

        # -- Concat clips (stream copy) -----------------------------------------
        job.update(progress=74, message="Assembling clips...")
        model_tag = model_name.split()[0].lower()
        concat_path = str(job_dir / f"zoom_{direction}_{model_tag}_{ts}.mp4")

        if len(clip_paths) == 1:
            shutil.copy2(clip_paths[0], concat_path)
        else:
            try:
                # Stream-copy concat: _chain_anchor has already trimmed every
                # clip and extracted its last frame as the next clip's start,
                # so the junction is frame-exact. Re-encoding (xfade etc.)
                # would introduce artifacts that compound through the chain.
                list_file = str(job_dir / "concat_list.txt")
                with open(list_file, "w", encoding="utf-8") as _f:
                    for cp in clip_paths:
                        _f.write(f"file '{cp}'\n")
                r = subprocess.run(
                    ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                     "-i", list_file, "-c", "copy", concat_path],
                    capture_output=True, timeout=120,
                )
                if r.returncode != 0 or not os.path.isfile(concat_path):
                    raise RuntimeError(r.stderr.decode(errors="replace")[-300:])
            except Exception as e:
                _log(f"[error] Concat failed: {e}")
                job.update(status="error", message=f"Concat failed: {e}")
                return

        # -- Prepend base video for extend/continue jobs --------------------------
        if extend_base_path and os.path.isfile(extend_base_path):
            job.update(progress=76, message="Joining with original...")
            extended_path = str(job_dir / f"zoom_{direction}_{model_tag}_{ts}_ext.mp4")
            try:
                ext_list = str(job_dir / "extend_list.txt")
                with open(ext_list, "w", encoding="utf-8") as _f:
                    _f.write(f"file '{extend_base_path}'\n")
                    _f.write(f"file '{concat_path}'\n")
                r_ext = subprocess.run(
                    ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                     "-i", ext_list, "-c:v", "copy", "-an", extended_path],
                    capture_output=True, timeout=180,
                )
                if r_ext.returncode == 0 and os.path.isfile(extended_path):
                    concat_path = extended_path
                    log.info("[zoom] Extended: %s", extended_path)
                else:
                    log.warning("[zoom] Extend concat failed -- using new clips only")
            except Exception as _ee:
                log.warning("[zoom] Extend concat error: %s -- using new clips only", _ee)

        if skip_audio:
            job.output = concat_path
            copy_to_inbox(concat_path)
            job.message = f"Zoom {direction} done ({len(clip_paths)} clips, no audio)"
            return

    finally:
        # Clean up chain frame PNGs -- they were only needed during generation
        for i in range(n_clips):
            frame_png = job_dir / f"frame_{i:02d}.png"
            if frame_png.exists():
                try:
                    frame_png.unlink()
                except Exception:
                    pass

    # -- Audio -----------------------------------------------------------------
    # Ensure audio_path / audio_err are always defined (NameError guard for the
    # case where audio_first=True but prep never wrote _audio_path).
    audio_path: str | None = None
    audio_err: str | None = None
    total_dur: float = 0.0

    # If audio-first succeeded, the audio is already in audio_first_path and
    # the clips were conditioned on it -- skip ACE-Step entirely.
    if audio_first and audio_first_path and os.path.isfile(audio_first_path):
        log.info("[zoom] Audio-first: reusing pre-generated audio, skipping ACE-Step")
        audio_path = audio_first_path
        audio_err = None
        total_dur = probe_duration(concat_path)
        # Clean up per-clip audio slices
        for _si in range(n_clips):
            _sp = job_dir / f"audio_slice_{_si:02d}.wav"
            if _sp.exists():
                try: _sp.unlink()
                except Exception: pass
    else:
        audio_first_path = None  # ensure we fall into the normal ACE-Step path

    if not audio_first_path:
        gpu.acquire("acestep", reason=f"zoom-{direction} music gen")

    if not audio_first_path and not music_prompt:
        job.update(progress=76, message="Getting music direction...")
        try:
            from features.fun_videos import analyzer
            frames = []
            if os.path.isfile(concat_path):
                from features.fun_videos.multi_pipeline import _sample_music_frames
                frames = _sample_music_frames(concat_path, llm_router)
            result = analyzer.generate_music_prompt(
                llm_router, frames, settings.get("idea", "")
            )
            music_prompt = result.get("music_prompt", "")
        except Exception as e:
            log.warning("[zoom] Post-video music analysis failed: %s", e)

    if not audio_first_path:
        if not music_prompt:
            zoom_defaults = {
                "out": "cinematic orchestral pullback, swelling strings, sense of expanding scale",
                "in": "intimate piano approach, quiet focus, detail emerging, gentle tension",
            }
            music_prompt = zoom_defaults[direction]

        if not instrumental and not lyrics:
            job.update(progress=79, message="Writing lyrics...")
            try:
                from features.fun_videos import analyzer
                lyrics = analyzer.generate_lyrics(llm_router, [], music_prompt, "")
            except Exception as e:
                log.warning("[zoom] Lyrics failed: %s", e)

        total_dur = probe_duration(concat_path)
        audio_dur = min(total_dur + 2.0, 300.0) if total_dur > 0 else n_clips * clip_dur + 2.0

        job.update(progress=82, message="Generating audio...")

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

    if not audio_path:
        _log(f"[warning] Audio failed: {audio_err} -- saving video only")
        job.output = concat_path
        copy_to_inbox(concat_path)
        job.message = f"Zoom {direction} done ({len(clip_paths)} clips, no audio)"
        return

    if _stopped():
        return

    # -- Merge -----------------------------------------------------------------
    job.update(progress=92, message="Merging video + audio...")
    final_path = str(job_dir / f"zoom_{direction}_{model_tag}_{ts}_final.mp4")
    merged = video_generator.merge_video_audio(concat_path, audio_path, final_path, log_fn=_log)

    if not merged:
        job.output = concat_path
        copy_to_inbox(concat_path)
        job.message = f"Zoom {direction} done (audio merge failed)"
        return

    # Optional upscale
    upscale_on = settings.get("upscale", False)
    if upscale_on and not _stopped():
        job.update(progress=95, message="Upscaling...")
        try:
            from core.upscaler import upscale_video
            up_path = str(job_dir / f"zoom_{direction}_{model_tag}_{ts}_up.mp4")
            up_out, _ = upscale_video(merged, up_path,
                                      scale=float(settings.get("upscale_scale", 2.0)),
                                      method=settings.get("upscale_method", "ffmpeg"))
            if up_out:
                merged = up_out
        except Exception as ue:
            log.warning("[zoom] Upscale failed: %s", ue)

    job.output = merged
    copy_to_inbox(merged)
    job.meta.update({"final_path": merged, "music_prompt": music_prompt,
                     "direction": direction, "clips": len(clip_paths)})
    job.message = f"Zoom {direction} complete! ({len(clip_paths)} clips)"
    try:
        from core.session import get_current as get_session
        get_session().add_file(Path(merged).name, "video", "zoom", path=merged)
    except Exception:
        pass

    # Gallery
    try:
        norm = merged.replace("\\", "/")
        idx = norm.lower().find("/output/")
        url = norm[idx:] if idx != -1 else f"/output/{Path(merged).name}"
        gallery_push(
            url, tab="zoom",
            prompt=(arc[0].get("prompt", "") if arc else "")[:120],
            model=model_name,
            metadata={
                "path": merged, "job_id": job.id, "direction": direction,
                "clips": len(clip_paths), "model": model_name,
                "duration_sec": total_dur or n_clips * clip_dur,
            },
        )
    except Exception as e:
        log.warning("[zoom] gallery_push failed: %s", e)
