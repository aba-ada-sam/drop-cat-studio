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
