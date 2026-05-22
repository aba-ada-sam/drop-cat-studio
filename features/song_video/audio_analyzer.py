"""Audio analysis for the Song Video feature.

Uses librosa for rich analysis (BPM, key, per-clip energy profile) when
available, falls back to ffprobe-only (duration + basic metadata) if not.
Install librosa with: pip install librosa
"""
import logging
import math
import os
import re
from pathlib import Path

from core.ffmpeg_utils import probe_duration

log = logging.getLogger(__name__)

NOTES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

# Bars per clip options, in preference order.
# We pick the first option that lands within WanGP's 8-20s range.
_BARS_PER_CLIP_OPTIONS = [8, 4, 16]


def _bars_to_seconds(bpm: float, bars: int) -> float:
    """Convert bars to seconds at the given BPM (4/4 time assumed)."""
    beats_per_second = bpm / 60.0
    return (bars * 4) / beats_per_second


def _suggest_clip_dur(bpm: float | None) -> int:
    """Pick a musically-aligned clip duration in seconds (8-10s range).

    Ceiling is 10s: LTX-2 quality degrades noticeably above 10 seconds --
    more frames means harder to maintain temporal coherence, producing
    blurry output that oscillates with the shorter clips around it.
    The 19s sliding-window threshold is a technical ceiling, not a
    quality target.
    """
    if bpm and bpm > 0:
        for bars in _BARS_PER_CLIP_OPTIONS:
            secs = _bars_to_seconds(bpm, bars)
            if 8 <= secs <= 10:
                return max(8, min(10, round(secs)))
    return 8  # fallback


def _energy_label(e: float) -> str:
    if e > 0.70:
        return "HIGH"
    if e > 0.35:
        return "MED"
    return "LOW"


def _mood_from_analysis(mode: str | None, energy: str) -> str:
    if mode == "minor":
        return "intense" if energy == "high" else ("melancholic" if energy == "low" else "dramatic")
    return "euphoric" if energy == "high" else ("peaceful" if energy == "low" else "uplifting")


def _place_boundaries(
    peak_times, peak_strengths, total_dur: float, n_clips: int,
    min_dur: float, max_dur: float,
) -> list[float]:
    """Shared boundary-placement logic used by both clip functions.

    Clamps each chosen boundary to <= total_dur so the final clip can never
    have a negative duration when n_clips * min_dur exceeds total_dur. The
    caller still gets n_clips boundaries, but the trailing ones may collapse
    to zero-length when the song is too short for the requested clip count.
    """
    import numpy as np
    boundaries = [0.0]
    for i in range(1, n_clips):
        prev  = boundaries[-1]
        ideal = total_dur * i / n_clips
        lo, hi = prev + min_dur, prev + max_dur
        mask = (peak_times >= lo) & (peak_times <= hi)
        if mask.any():
            cands     = peak_times[mask]
            strengths = peak_strengths[mask]
            proximity = 1.0 / (1.0 + np.abs(cands - ideal))
            chosen    = float(cands[np.argmax(strengths * proximity)])
        else:
            chosen = max(lo, min(hi, ideal))
        boundaries.append(min(chosen, total_dur))
    last_end = min(total_dur, boundaries[-1] + max_dur)
    boundaries.append(max(last_end, boundaries[-1]))
    return boundaries


def compute_clip_plan(
    audio_path: str,
    n_clips: int,
    min_dur: float = 8.0,
    max_dur: float = 10.0,
) -> tuple[list[float], list[float]]:
    """Beat-aligned clip plan with per-clip peak-beat positions.

    Returns:
        durations      list[float]  clip lengths in seconds, with each boundary
                                    snapped to the nearest strong onset within
                                    [min_dur, max_dur] of the previous boundary.
        beat_positions list[float]  0.0-1.0 position of the strongest onset
                                    within each clip (0 = start, 1 = end).
                                    The pipeline post-warps each generated clip
                                    so its visual peak lands at this position.
    """
    total_dur = probe_duration(audio_path)
    if total_dur <= 0 or n_clips <= 0:
        return [min_dur] * max(1, n_clips), [0.5] * max(1, n_clips)

    equal_dur   = total_dur / n_clips
    default_dur = max(min_dur, min(max_dur, equal_dur))
    default_durs  = [round(default_dur, 3)] * n_clips
    default_beats = [0.5] * n_clips

    try:
        import librosa
        import numpy as np

        y, sr = librosa.load(audio_path, sr=22050, mono=True, duration=min(total_dur, 300))
        onset_env  = librosa.onset.onset_strength(y=y, sr=sr)
        min_frames = int(sr / 512 * min_dur * 0.5)
        peaks = librosa.util.peak_pick(
            onset_env,
            pre_max=4, post_max=4, pre_avg=4, post_avg=8,
            delta=0.4, wait=max(1, min_frames),
        )
        if len(peaks) < 2:
            return default_durs, default_beats

        peak_times     = librosa.frames_to_time(peaks, sr=sr)
        peak_strengths = onset_env[peaks]
        boundaries     = _place_boundaries(peak_times, peak_strengths, total_dur, n_clips, min_dur, max_dur)
        durations      = [round(boundaries[j + 1] - boundaries[j], 3) for j in range(n_clips)]

        # Where does the strongest onset fall WITHIN each clip? (0.0 = start, 1.0 = end)
        beat_positions: list[float] = []
        for j in range(n_clips):
            clip_start = boundaries[j]
            clip_end   = boundaries[j + 1]
            clip_len   = clip_end - clip_start
            mask = (peak_times >= clip_start) & (peak_times < clip_end)
            if mask.any():
                cands    = peak_times[mask]
                strs     = peak_strengths[mask]
                hit_time = float(cands[np.argmax(strs)])
                pos      = (hit_time - clip_start) / clip_len if clip_len > 0 else 0.5
                beat_positions.append(round(max(0.0, min(1.0, pos)), 2))
            else:
                beat_positions.append(0.5)

        log.info("[song-video] Clip plan: durations=%s beat_pos=%s", durations, beat_positions)
        return durations, beat_positions

    except Exception as e:
        log.warning("[song-video] Clip plan failed (%s) -- using equal durations", e)
        return default_durs, default_beats


_WHISPER_NOISE = re.compile(
    '\\[.*?\\]|\\(.*?\\)|\u266a|\u266b|\\bmm+\\b|\\buh+\\b|\\bah+\\b',
    re.IGNORECASE,
)


def _transcribe_lyrics(audio_path: str, max_seconds: float = 120.0) -> str:
    """Transcribe up to max_seconds of audio using faster-whisper.

    Returns cleaned lyric text, or empty string if unavailable/instrumental.
    First call downloads the base model (~74 MB, cached afterwards).
    """
    try:
        from faster_whisper import WhisperModel
        log.info("[song-video] Transcribing lyrics (faster-whisper base)...")
        model = WhisperModel("base", device="cpu", compute_type="int8")
        segments, _info = model.transcribe(
            audio_path,
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
        )
        lines: list[str] = []
        for seg in segments:
            if seg.start > max_seconds:
                break
            cleaned = _WHISPER_NOISE.sub("", seg.text).strip()
            if cleaned:
                lines.append(cleaned)
        text = " ".join(lines).strip()
        log.info("[song-video] Lyrics detected: %d chars", len(text))
        return text
    except ImportError:
        log.debug("[song-video] faster-whisper not installed -- skipping lyric detection")
        return ""
    except Exception as e:
        log.warning("[song-video] Lyric transcription failed: %s", e)
        return ""


def analyze(audio_path: str, suggested_clip_dur: int | None = None) -> dict:
    """Analyze an audio file and return a structured analysis dict.

    Returns:
        duration          float   total seconds
        duration_display  str     "M:SS"
        bpm               int|None  beats per minute (None if librosa unavailable)
        key               str|None  e.g. "A"
        mode              str|None  "major" | "minor"
        mood              str     derived from mode + energy
        energy            str     "low" | "moderate" | "high"
        energy_profile    list    per-clip energy 0.0-1.0 (empty if no librosa)
        clip_energy_labels list   ["HIGH", "MED", "LOW", ...]
        suggested_clip_dur int    BPM-aligned clip length in seconds
        suggested_num_clips int   ceil(duration / suggested_clip_dur)
        has_rich_analysis bool    True if librosa ran successfully
        lyrics_text       str     AI-transcribed lyrics (first ~2 min), may be empty
    """
    result: dict = {
        "duration": 0.0,
        "duration_display": "0:00",
        "bpm": None,
        "key": None,
        "mode": None,
        "mood": "",
        "energy": "moderate",
        "energy_profile": [],
        "clip_energy_labels": [],
        "suggested_clip_dur": suggested_clip_dur or 8,
        "suggested_num_clips": 0,
        "has_rich_analysis": False,
        "lyrics_text": "",
        "beat_times": [],
        "beat_strengths": [],
    }

    # -- Basic duration via ffprobe ----------------------------------------
    dur = probe_duration(audio_path)
    if dur <= 0:
        log.warning("[song-video] Could not probe duration for %s", audio_path)
        return result

    result["duration"] = dur
    mins, secs = divmod(int(dur), 60)
    result["duration_display"] = f"{mins}:{secs:02d}"

    # -- Rich analysis via librosa -----------------------------------------
    try:
        import librosa
        import numpy as np

        log.info("[song-video] Running librosa analysis on %s", Path(audio_path).name)
        # Load mono, 22050 Hz -- good enough for beat/key analysis, fast to load
        y, sr = librosa.load(audio_path, sr=22050, mono=True, duration=min(dur, 300))

        # BPM + beat timestamps.
        # beat_track returns a scalar in librosa 0.10+ and a 1-element ndarray in 0.9.x.
        tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(np.atleast_1d(tempo)[0])
        result["bpm"] = max(40, min(240, round(bpm)))

        all_beat_times = librosa.frames_to_time(beat_frames, sr=sr)
        # Pair each beat with its onset strength for flash weighting later.
        onset_env_full = librosa.onset.onset_strength(y=y, sr=sr)
        bt_strengths = []
        for bf in beat_frames:
            idx = int(min(bf, len(onset_env_full) - 1))
            bt_strengths.append(float(onset_env_full[idx]))
        max_str = max(bt_strengths) if bt_strengths else 1.0
        result["beat_times"]     = [round(float(t), 3) for t in all_beat_times if float(t) < dur]
        result["beat_strengths"] = [round(s / max_str, 3) for s in bt_strengths[:len(result["beat_times"])]]

        # Key via chroma
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
        chroma_mean = np.mean(chroma, axis=1)
        key_idx = int(np.argmax(chroma_mean))
        result["key"] = NOTES[key_idx]

        # Major vs minor: compare prominence of minor-third vs major-third above tonic
        minor_third = (key_idx + 3) % 12
        major_third = (key_idx + 4) % 12
        result["mode"] = "minor" if chroma_mean[minor_third] > chroma_mean[major_third] else "major"

        # Per-clip energy profile (RMS)
        clip_dur_secs = suggested_clip_dur or _suggest_clip_dur(bpm)
        result["suggested_clip_dur"] = clip_dur_secs
        n_clips = max(1, math.ceil(dur / clip_dur_secs))

        rms = librosa.feature.rms(y=y)[0]
        samples_per_clip = max(1, len(rms) // n_clips)
        energy_raw = []
        for i in range(n_clips):
            start = i * samples_per_clip
            end = min(start + samples_per_clip, len(rms))
            energy_raw.append(float(np.mean(rms[start:end])) if end > start else 0.0)

        max_e = max(energy_raw) if max(energy_raw) > 0 else 1.0
        energy_profile = [round(e / max_e, 3) for e in energy_raw]
        result["energy_profile"] = energy_profile
        result["clip_energy_labels"] = [_energy_label(e) for e in energy_profile]

        # Overall energy level (label)
        overall_rms = float(np.mean(rms))
        peak_rms    = float(np.max(rms)) if np.max(rms) > 0 else 1.0
        ratio = overall_rms / peak_rms
        result["energy"] = "high" if ratio > 0.45 else ("low" if ratio < 0.20 else "moderate")

        result["mood"] = _mood_from_analysis(result["mode"], result["energy"])
        result["has_rich_analysis"] = True
        log.info(
            "[song-video] Analysis done: %d BPM, %s %s, %s energy, %d clips x %ds",
            result["bpm"], result["key"], result["mode"],
            result["energy"], n_clips, clip_dur_secs,
        )

    except ImportError:
        log.info("[song-video] librosa not installed -- using ffprobe-only analysis. "
                 "Run: pip install librosa  for BPM/key/energy features.")
        result["suggested_clip_dur"] = suggested_clip_dur or 8

    except Exception as e:
        log.warning("[song-video] librosa analysis failed: %s", e)
        result["suggested_clip_dur"] = suggested_clip_dur or 8

    # -- Always recalculate clip count from final clip dur -----------------
    result["suggested_num_clips"] = max(1, math.ceil(dur / result["suggested_clip_dur"]))
    if not result["mood"]:
        result["mood"] = _mood_from_analysis(result.get("mode"), result["energy"])

    return result
