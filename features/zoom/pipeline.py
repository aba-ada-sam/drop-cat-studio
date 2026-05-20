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

from core.ffmpeg_utils import probe_duration
from core.llm_client import TIER_BALANCED, TIER_FAST, encode_image_b64, parse_json_response
from core.llm_router import LLMRouter
from features.fun_videos.multi_pipeline import _chain_anchor, _CHAIN_TRIM_RATIO

log = logging.getLogger("zoom")

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_ZOOM_OUT_SYSTEM = """\
You are planning a smooth continuous zoom-out video from a still photograph.
The camera pulls back across {n} sequential clips. Each clip starts from the
last frame of the previous -- the audience sees one unbroken retreating move.

TASK: Describe what the camera sees at each stage as it retreats, grounded
in what is plausible given the photo. Do not invent elements inconsistent
with the visible scene (lighting, season, setting).

RULES:
- Every prompt MUST open with the subject anchor: exact colour, material, shape.
- State the zoom level: "fills 90% of frame" / "50% of frame" / "25%" / "tiny figure".
- Name what appears at the frame edges as the camera retreats (walls, sky, trees, etc.).
- Close every prompt with: "Camera pulls back steadily -- smooth fluid motion, no shake."
- Prompts 50-70 words each. Vary duration: action=4-5s, atmosphere=6-8s.
- Do NOT use negative words (avoid "no X", "without X") -- use positive description only.

Return ONLY valid JSON, no other text:
{"subject_anchor": "exact one-line description", "clips": [{"prompt": "...", "duration": 5}, ...]}
"""

_ZOOM_IN_SYSTEM = """\
You are planning a smooth continuous zoom-in video from a still photograph.
The camera pushes forward across {n} sequential clips. Each clip starts from
the last frame of the previous -- one unbroken advancing move.

TASK: Choose ONE compelling focal target visible in the photo (a face, an
object, a texture). Describe what the camera sees at each stage as it
advances toward that target.

RULES:
- Every prompt MUST open with the subject anchor: exact colour, material, shape.
- Name the focal target explicitly every clip.
- State zoom progress: "full frame" / "upper body fills frame" / "face fills frame" / "eye fills frame".
- Name what leaves the frame edges as the camera advances.
- Close every prompt with: "Camera pushes in steadily -- smooth fluid motion, no shake."
- Prompts 50-70 words each. Later clips shorter (5-6s) as detail gets tighter.
- Do NOT use negative words -- positive description only.

Return ONLY valid JSON, no other text:
{"subject_anchor": "exact one-line description", "focal_target": "what camera zooms toward", "clips": [{"prompt": "...", "duration": 5}, ...]}
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
    system = system_tpl.format(n=n_clips)

    idea_line = f"\nUser's idea: {idea}" if idea else ""
    user_msg = (
        f"Direction: zoom {direction}\n"
        f"Clips: {n_clips}\n"
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
        if not data:
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
            try:
                dur = max(4.0, min(10.0, float(item.get("duration", 5.0))))
            except (TypeError, ValueError):
                dur = 5.0
            clips.append({"prompt": prompt, "duration": dur})
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
                    tier=TIER_BALANCED, system=system, max_tokens=1200,
                    format_json=True,
                )
            else:
                text = llm_router.route(
                    user_msg, tier=TIER_BALANCED, system=system, max_tokens=1200,
                    format_json=True,
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
    return _fallback_arc(direction, n_clips, idea), "", ""


def _fallback_arc(direction: str, n_clips: int, idea: str) -> tuple[list[dict], str, str]:
    idea_prefix = f"{idea}. " if idea else ""
    if direction == "out":
        phrases = [
            "Subject fills the entire frame. Camera begins smooth steady pullback.",
            "Subject now fills half the frame, environment visible at edges. Camera retreats.",
            "Subject occupies a quarter of the frame, full surroundings visible. Camera pulls back.",
            "Subject is a small figure in the wide environment. Camera continues retreating.",
            "Establishing wide shot, subject small, full context of location revealed.",
            "Extreme wide, subject barely visible, grand scale of environment established.",
        ]
        verb = "Camera pulls back steadily -- smooth fluid motion, no shake."
    else:
        phrases = [
            "Full scene visible, subject centered. Camera begins slow steady push forward.",
            "Subject fills half the frame, surroundings receding. Camera advances.",
            "Upper body fills the frame, detail beginning to emerge. Camera pushes in.",
            "Face or central feature fills the frame, texture visible. Camera advances.",
            "Extreme close-up, fine detail fills the frame, surrounding falls away.",
            "Macro detail, texture and surface visible in full clarity.",
        ]
        verb = "Camera pushes in steadily -- smooth fluid motion, no shake."

    clips = []
    for i in range(n_clips):
        phrase = phrases[min(i, len(phrases) - 1)]
        clips.append({
            "prompt": f"{idea_prefix}{phrase} {verb}",
            "duration": 5.0,
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

    job.update(progress=10, message="Arc ready, waiting for GPU...")


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
    model_name = settings.get("model_name", "Wan2.1-I2V-14B-480P")
    skip_audio = settings.get("skip_audio", False)
    instrumental = settings.get("instrumental", False)
    music_prompt = settings.pop("_prepped_music_prompt", settings.get("music_prompt", ""))
    lyrics = settings.pop("_prepped_lyrics", settings.get("lyrics", ""))
    arc: list[dict] = settings.pop("_zoom_arc", [])
    subject_anchor: str = settings.pop("_zoom_subject_anchor", "")
    focal_target: str = settings.pop("_zoom_focal_target", "")

    # Build fallback arc if prep didn't run
    if not arc:
        arc, subject_anchor, focal_target = _fallback_arc(direction, n_clips, settings.get("idea", ""))

    def _stopped() -> bool:
        return job.stop_event.is_set()

    # Output directory
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_root = Path(source_path).parent.parent / time.strftime("%Y-%m-%d")
    job_tag = f"zoom_{direction}_{Path(source_path).stem[:12]}_{ts}"
    job_dir = out_root / job_tag
    job_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(source_path, job_dir / f"source{Path(source_path).suffix}")

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

            # Resize start image to match model resolution
            start_img = prev_frame or source_path
            try:
                from features.fun_videos.pipeline import _prep_photo
                prepped = _prep_photo(start_img, model_name)
            except Exception:
                prepped = start_img

            ok = False
            for attempt in range(2):
                if _stopped():
                    return
                try:
                    result = video_generator.generate_video(
                        photo_path=prepped,
                        output_path=clip_out,
                        prompt=prompt,
                        model_name=model_name,
                        steps=wangp_steps,
                        guidance=wangp_guidance,
                        duration=gen_dur,
                        seed=-1,
                        stop_event=job.stop_event,
                        progress_cb=lambda cur, tot: job.update(
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

        # -- Concat clips -------------------------------------------------------
        job.update(progress=74, message="Assembling clips...")
        model_tag = model_name.split()[0].lower()
        concat_path = str(job_dir / f"zoom_{direction}_{model_tag}_{ts}.mp4")

        if len(clip_paths) == 1:
            shutil.copy2(clip_paths[0], concat_path)
        else:
            try:
                list_file = str(job_dir / "concat_list.txt")
                with open(list_file, "w", encoding="utf-8") as f:
                    for cp in clip_paths:
                        f.write(f"file '{cp}'\n")
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
    gpu.acquire("acestep", reason=f"zoom-{direction} music gen")

    if not music_prompt:
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
