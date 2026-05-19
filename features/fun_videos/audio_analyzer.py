"""Audio transcription and beat/energy analysis for music-video clip sync.

faster-whisper transcribes generated audio to get timed lyric segments.
librosa detects tempo, beat timestamps, and energy peaks so clip cut points
can be snapped to musical moments.

Both run CPU-only -- no VRAM contention with WanGP or ACE-Step.
"""
import logging

log = logging.getLogger(__name__)


def transcribe_audio(audio_path: str, language: str = "en") -> list[dict]:
    """Transcribe audio with faster-whisper tiny model.

    Returns list of {start, end, text} dicts, one per spoken segment.
    Uses int8 quantisation so it runs quickly on CPU without significant RAM.
    Empty list on failure -- callers must handle the no-transcript case gracefully.
    """
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("tiny", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(audio_path, language=language, word_timestamps=False)
        result = [
            {"start": float(seg.start), "end": float(seg.end), "text": seg.text.strip()}
            for seg in segments
            if seg.text.strip()
        ]
        log.info("[audio_analyzer] Transcribed %d segments from %s", len(result), audio_path)
        return result
    except Exception as e:
        log.warning("[audio_analyzer] Transcription failed: %s", e)
        return []


def detect_audio_events(audio_path: str) -> dict:
    """Analyse audio structure with librosa.

    Returns:
        bpm           -- detected tempo (float)
        beat_times    -- list of beat timestamps in seconds
        energy_peaks  -- list of timestamps where RMS energy has a local maximum
                         above the 75th percentile, spaced at least 2s apart
        sections      -- list of {start, end, energy} dicts (normalised 0-1)
                         divides the track into 4-8 broad structural segments
        duration      -- track length in seconds
    """
    try:
        import librosa
        import numpy as np

        y, sr = librosa.load(audio_path, sr=22050, mono=True)
        duration = float(librosa.get_duration(y=y, sr=sr))

        tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
        beat_times = librosa.frames_to_time(beat_frames, sr=sr).tolist()

        hop_length = 512
        rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
        times = librosa.times_like(rms, sr=sr, hop_length=hop_length)

        # Local-maxima energy peaks above 75th percentile, min 2s apart
        threshold = float(np.percentile(rms, 75))
        energy_peaks: list[float] = []
        for i in range(1, len(rms) - 1):
            if (rms[i] > threshold
                    and float(rms[i]) >= float(rms[i - 1])
                    and float(rms[i]) >= float(rms[i + 1])):
                t = float(times[i])
                if not energy_peaks or (t - energy_peaks[-1]) >= 2.0:
                    energy_peaks.append(t)

        # Divide track into 4-8 broad sections
        n_sections = min(8, max(4, int(duration / 15)))
        sec_dur = duration / n_sections
        sections = []
        for i in range(n_sections):
            t0 = i * sec_dur
            t1 = min((i + 1) * sec_dur, duration)
            mask = (times >= t0) & (times < t1)
            avg_e = float(np.mean(rms[mask])) if mask.any() else 0.0
            sections.append({"start": t0, "end": t1, "energy": avg_e})

        max_e = max(s["energy"] for s in sections) or 1.0
        for s in sections:
            s["energy"] = round(s["energy"] / max_e, 3)

        log.info("[audio_analyzer] BPM=%.1f, %d beats, %d peaks, %d sections, dur=%.1fs",
                 float(tempo), len(beat_times), len(energy_peaks), n_sections, duration)
        return {
            "bpm": float(tempo),
            "beat_times": beat_times,
            "energy_peaks": energy_peaks,
            "sections": sections,
            "duration": duration,
        }
    except Exception as e:
        log.warning("[audio_analyzer] Beat analysis failed: %s", e)
        return {"bpm": 0.0, "beat_times": [], "energy_peaks": [], "sections": [], "duration": 0.0}


def build_clip_audio_context(
    transcript: list[dict],
    audio_events: dict,
    n_clips: int,
    clip_durations: list[float],
    audio_duration: float,
) -> list[dict]:
    """Map audio analysis onto per-clip time windows.

    Divides the audio track proportionally to planned clip durations
    and returns, for each clip:
        lyric_text      -- words sung during this clip's time window
        energy_level    -- normalised average energy (0=quiet, 1=loudest section)
        has_energy_peak -- True if a detected energy peak falls in this window
        time_start      -- window start in seconds
        time_end        -- window end in seconds
    """
    if not audio_duration or audio_duration <= 0:
        return [
            {"lyric_text": "", "energy_level": 0.5, "has_energy_peak": False,
             "time_start": 0.0, "time_end": 0.0}
            for _ in range(n_clips)
        ]

    total_planned = sum(clip_durations[:n_clips]) or (n_clips * 5.0)
    sections = audio_events.get("sections", [])
    peaks = audio_events.get("energy_peaks", [])

    result = []
    cursor = 0.0
    for i in range(n_clips):
        dur = clip_durations[i] if i < len(clip_durations) else 5.0
        t_start = cursor
        t_end = min(cursor + (dur / total_planned) * audio_duration, audio_duration)
        cursor = t_end

        lyrics_in_window = [
            seg["text"] for seg in transcript
            if seg["end"] > t_start and seg["start"] < t_end
        ]

        energy_values = []
        for sec in sections:
            overlap = min(sec["end"], t_end) - max(sec["start"], t_start)
            if overlap > 0:
                energy_values.append(sec["energy"])
        energy_level = sum(energy_values) / len(energy_values) if energy_values else 0.5

        has_peak = any(t_start <= p <= t_end for p in peaks)

        result.append({
            "lyric_text": " ".join(lyrics_in_window).strip(),
            "energy_level": round(energy_level, 2),
            "has_energy_peak": has_peak,
            "time_start": round(t_start, 1),
            "time_end": round(t_end, 1),
        })

    return result


def snap_durations_to_beats(
    story_arc: list[dict],
    audio_events: dict,
    audio_duration: float,
    snap_window: float = 2.0,
) -> list[dict]:
    """Adjust clip durations so boundaries land on strong musical beats.

    For each clip boundary (all but the last), finds the nearest energy peak
    or strong beat within snap_window seconds and shifts the boundary there.
    Returns a new story_arc list with adjusted durations; total duration is
    preserved within ~snap_window seconds.

    Clips shorter than 3s after snapping are left at their original duration
    to avoid creating unusably short clips.
    """
    if not story_arc or audio_duration <= 0:
        return story_arc

    # Build a sorted list of all candidate snap points (energy peaks + every 4th beat)
    peaks = audio_events.get("energy_peaks", [])
    beats = audio_events.get("beat_times", [])
    # Every 4th beat is a strong beat (downbeat approximation)
    strong_beats = beats[::4] if beats else []
    candidates = sorted(set(peaks + strong_beats))
    if not candidates:
        return story_arc

    # Compute current cumulative boundaries
    boundaries: list[float] = []
    t = 0.0
    for clip in story_arc:
        dur = float(clip.get("duration", 5.0)) if isinstance(clip, dict) else 5.0
        t += dur
        boundaries.append(t)

    # Snap each interior boundary (skip the last -- it's the video end)
    snapped: list[float] = list(boundaries)
    for i in range(len(boundaries) - 1):
        b = boundaries[i]
        best = min(candidates, key=lambda c: abs(c - b))
        if abs(best - b) <= snap_window:
            snapped[i] = best

    # Rebuild durations from snapped boundaries
    new_arc = []
    prev = 0.0
    for i, clip in enumerate(story_arc):
        new_dur = snapped[i] - prev
        if new_dur < 3.0:
            new_dur = float(clip.get("duration", 5.0)) if isinstance(clip, dict) else 5.0
        entry = dict(clip) if isinstance(clip, dict) else {"prompt": str(clip), "duration": 5.0}
        entry["duration"] = round(new_dur, 2)
        new_arc.append(entry)
        prev = snapped[i]

    log.info("[audio_analyzer] Beat-snapped %d clip boundaries (snap_window=%.1fs)",
             len(story_arc) - 1, snap_window)
    return new_arc
