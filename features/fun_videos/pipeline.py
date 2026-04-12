"""Full Fun Videos pipeline: photo -> video -> audio -> merge.

Orchestrates the analyzer, video_generator, and audio_generator modules
into a single job that the job manager can execute.
"""
import logging
import os
import shutil
import time
from pathlib import Path

from core import config as cfg
from core.ffmpeg_utils import probe_duration, extract_frame_b64
from core.wildcards import expand

log = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"


def run_pipeline(job, photo_path, settings):
    """Full pipeline worker function for JobManager.

    Settings keys:
        video_prompt, music_prompt, lyrics, user_direction,
        use_wildcards, video_duration, model_name, resolution,
        video_steps, video_guidance, video_seed,
        audio_steps, audio_guidance, instrumental, audio_format,
        bpm, skip_audio, end_photo_path
    """
    from app import get_llm_router; llm_router = get_llm_router()
    from features.fun_videos import analyzer, video_generator, audio_generator

    # Setup
    ts = time.strftime("%Y-%m-%d")
    if photo_path:
        slug = Path(photo_path).stem[:20].replace(" ", "_")
    else:
        # Text-to-video mode — slug from prompt
        video_prompt_raw = settings.get("video_prompt", "")
        slug = "t2v_" + "".join(c if c.isalnum() else "_" for c in video_prompt_raw[:16]).strip("_")
    job_dir = OUTPUT_DIR / ts / f"{slug}_{job.id}"
    job_dir.mkdir(parents=True, exist_ok=True)

    video_prompt = settings.get("video_prompt", "")
    music_prompt = settings.get("music_prompt", "")
    lyrics = settings.get("lyrics", "")
    use_wildcards = settings.get("use_wildcards", False)
    skip_audio = settings.get("skip_audio", False)
    user_direction = settings.get("user_direction", "")

    def _log(msg):
        log.info(msg)
        job.update(message=msg.lstrip("[info] ").lstrip("[error] ").lstrip("[success] "))

    def _stopped():
        return job.stop_event.is_set()

    # Copy source photo (optional — None for text-to-video)
    if photo_path:
        src_copy = job_dir / f"source{Path(photo_path).suffix}"
        shutil.copy2(photo_path, src_copy)

    # Expand wildcards
    fs_root = cfg.get("sd_wildcards_dir") or ""
    if use_wildcards and video_prompt:
        video_prompt = expand(video_prompt, fs_root)
    if use_wildcards and music_prompt:
        music_prompt = expand(music_prompt, fs_root)

    # ── Phase 1: Video Generation ────────────────────────────────────────
    job.update(progress=10, message="Generating video...")

    def _video_progress(step, total_steps):
        # Map inference steps into the 10–58% range so we don't collide with
        # the hard 60% marker set when generation finishes.
        pct = 10 + int(step / total_steps * 48) if total_steps > 0 else 10
        job.update(progress=pct, message=f"Generating video... step {step}/{total_steps}")

    video_path = video_generator.generate_video(
        image_path=photo_path,
        prompt=video_prompt,
        out_path=str(job_dir / f"video_{job.id[:8]}.mp4"),
        duration=float(settings.get("video_duration", 14.0)),
        model_name=settings.get("model_name", "LTX-2 Dev19B Distilled"),
        resolution=settings.get("resolution", "580p"),
        steps=int(settings.get("video_steps", 30)),
        guidance=float(settings.get("video_guidance", 7.5)),
        seed=int(settings.get("video_seed", -1)),
        end_image_path=settings.get("end_photo_path"),
        stop_check=_stopped,
        log_fn=_log,
        progress_fn=_video_progress,
    )

    if _stopped():
        return
    if not video_path:
        raise RuntimeError("Video generation failed")

    job.update(progress=60, message="Video generated!")
    job.meta["video_path"] = video_path
    job.meta["video_prompt"] = video_prompt

    # ── Early exit for video-only ────────────────────────────────────────
    if skip_audio:
        job.output = video_path
        job.message = "Video generated (no audio)"
        return

    # ── Phase 2: Music Prompt Generation ─────────────────────────────────
    job.update(progress=65, message="Analyzing video for music...")

    if not music_prompt:
        try:
            frames = []
            for pos in [0.1, 0.3, 0.5, 0.7, 0.9]:
                b64 = extract_frame_b64(video_path, position=pos, max_dim=512)
                if b64:
                    frames.append(b64)
            if frames:
                music_result = analyzer.generate_music_prompt(llm_router, frames, user_direction)
                music_prompt = music_result.get("music_prompt", "cinematic ambient, warm strings")
                if not settings.get("bpm") and music_result.get("bpm"):
                    settings["bpm"] = music_result["bpm"]
        except Exception as e:
            _log(f"[warning] Music analysis failed: {e}")
            music_prompt = "cinematic ambient, warm strings, gentle piano"

    if use_wildcards and music_prompt:
        music_prompt = expand(music_prompt, fs_root)

    # ── Phase 2b: Auto-generate lyrics if needed ──────────────────────────
    instrumental = settings.get("instrumental", False)
    if not instrumental and not lyrics:
        job.update(progress=68, message="Writing lyrics...")
        try:
            frames = []
            for pos in [0.1, 0.4, 0.7]:
                b64 = extract_frame_b64(video_path, position=pos, max_dim=512)
                if b64:
                    frames.append(b64)
            if frames:
                lyrics = analyzer.generate_lyrics(llm_router, frames, music_prompt, user_direction)
                if lyrics:
                    _log("[info] Auto-generated lyrics")
        except Exception as e:
            _log(f"[warning] Lyrics generation failed: {e}")

    # ── Phase 3: Audio Generation ────────────────────────────────────────
    job.update(progress=70, message="Generating audio...")

    video_dur = probe_duration(video_path)
    audio_dur = min(video_dur + 2.0, 120.0) if video_dur > 0 else 30.0

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
    )

    if _stopped():
        return

    if not audio_path:
        _log(f"[warning] Audio failed: {audio_err} — returning video only")
        job.output = video_path
        job.message = f"Video generated (audio failed: {audio_err})"
        return

    job.update(progress=85, message="Audio generated!")
    job.meta["audio_path"] = audio_path
    job.meta["music_prompt"] = music_prompt

    # ── Phase 4: Merge ───────────────────────────────────────────────────
    job.update(progress=90, message="Merging video + audio...")

    model_tag = settings.get("model_name", "ltx2").split()[0].lower()
    final_path = str(job_dir / f"fun_{model_tag}_{time.strftime('%H%M%S')}.mp4")

    merged = video_generator.merge_video_audio(video_path, audio_path, final_path, log_fn=_log)

    from core.session import get_current as get_session
    if merged:
        job.output = merged
        job.meta["final_path"] = merged
        job.message = "Complete! Video + audio merged"
        try:
            get_session().add_file(Path(merged).name, "video", "fun_videos", path=merged)
        except Exception as e:
            log.warning("session.add_file failed (video still generated): %s", e)
    else:
        job.output = video_path
        job.message = "Video generated (merge failed, returning video only)"
        try:
            get_session().add_file(Path(video_path).name, "video", "fun_videos", path=video_path)
        except Exception as e:
            log.warning("session.add_file failed (video still generated): %s", e)
