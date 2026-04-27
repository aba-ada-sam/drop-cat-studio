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

# Quality suffixes appended to every video prompt before sending to WanGP.
# These are model-family specific tags the models were trained on.
_PROMPT_SUFFIXES = {
    "ltx":  "cinematic depth blur, shallow depth of field, film grain, high quality, photorealistic motion",
    "wan":  "cinematic motion, smooth animation, photorealistic, high quality, detailed",
}

def _finalize_prompt(prompt: str, model_name: str) -> str:
    """Append model-appropriate quality suffix to any video prompt."""
    base = (prompt or "").strip().rstrip(".,;")
    key = "ltx" if "ltx" in model_name.lower() else "wan"
    suffix = _PROMPT_SUFFIXES[key]
    return f"{base}, {suffix}" if base else suffix


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
    lyric_direction = settings.get("lyric_direction", "")  # user's lyric theme/guideline
    use_wildcards = settings.get("use_wildcards", False)
    skip_audio = settings.get("skip_audio", False)
    user_direction = settings.get("user_direction", "")

    _last_error = [None]

    def _log(msg):
        log.info(msg)
        if "[error]" in msg:
            _last_error[0] = msg.replace("[error] ", "")
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

    video_prompt = _finalize_prompt(video_prompt, settings.get("model_name", ""))

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
        loras=settings.get("loras", []),
        stop_check=_stopped,
        log_fn=_log,
        progress_fn=_video_progress,
    )

    if _stopped():
        return
    if not video_path:
        reason = _last_error[0] or "WanGP worker not running — check Settings and start WanGP"
        raise RuntimeError(f"Video generation failed: {reason}")

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
    lyrics = ""
    if not instrumental:
        job.update(progress=68, message="Writing lyrics...")
        try:
            frames = []
            for pos in [0.1, 0.4, 0.7]:
                b64 = extract_frame_b64(video_path, position=pos, max_dim=512)
                if b64:
                    frames.append(b64)
            if frames:
                lyrics = analyzer.generate_lyrics(llm_router, frames, music_prompt, lyric_direction or user_direction)
                if lyrics:
                    _log("[info] Auto-generated lyrics")
        except Exception as e:
            _log(f"[warning] Lyrics generation failed: {e}")
        # Fallback: minimal structure so ACE-Step still renders vocals
        if not lyrics:
            lyrics = "[verse]\nSomething moves through the frame\nNothing stays the same\n[chorus]\nLife in motion\nSlipping through the frame"
            _log("[info] Using fallback lyrics")

    # ── Phase 3: Audio Generation ────────────────────────────────────────
    job.update(progress=70, message="Generating audio...")

    from services.forge_client import unload_checkpoint, reload_checkpoint
    forge_was_unloaded = unload_checkpoint()

    video_dur = probe_duration(video_path)
    audio_dur = min(video_dur + 2.0, 120.0) if video_dur > 0 else 30.0

    def _audio_progress(elapsed_s):
        job.update(progress=70 + min(14, elapsed_s // 10),
                   message=f"Generating audio... {elapsed_s}s elapsed")

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
        # Return both: raw video first, ACE-Step mixed second
        job.output = [video_path, merged]
        job.meta["final_path"] = merged
        job.message = "Complete!"
        try:
            get_session().add_file(Path(merged).name, "video", "fun_videos", path=merged)
        except Exception as e:
            log.warning("session.add_file failed: %s", e)
    else:
        job.output = video_path
        job.message = "Video generated (audio merge failed)"
        try:
            get_session().add_file(Path(video_path).name, "video", "fun_videos", path=video_path)
        except Exception as e:
            log.warning("session.add_file failed: %s", e)
