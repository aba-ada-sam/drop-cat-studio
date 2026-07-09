"""Vocal-activity detection + gating for lip sync.

A song is mostly instrumental. Both lip-sync paths we have (MuseTalk's post-pass
and LTX-2's native audio conditioning) drive the mouth from whatever energy is
in the driving track. Even after Demucs isolates the vocal stem, that stem still
carries residual bleed -- cymbals, guitar, kick -- through every instrumental
bar. The model turns that bleed into mouth movement, so the character sings to
the background music instead of the words.

This module finds the intervals where someone is actually singing:

    voiced_intervals()  -> [(start, end), ...] seconds
    gate_audio()        -> copy of the stem, hard-zeroed outside those intervals
    enable_expr()       -> ffmpeg `enable=` expression covering those intervals

Gating the driving audio makes the model see true digital silence between
phrases (a speech-trained model closes the mouth on silence), and the enable
expression lets the caller composite the ORIGINAL, untouched frames back over
the instrumental stretches so the mouth is provably still.

Detection uses Silero VAD (already vendored inside faster-whisper, which the
song pipeline uses for lyrics) run on the isolated stem, falling back to a
short-time energy gate if faster-whisper cannot be imported. Measured on a
3:30 track whose sung words -- per Whisper word timestamps -- cover 114.7s:
Silero-on-stem found 106.1s, the energy fallback 126.0s. Both land on the same
phrases; the energy gate is deliberately the looser of the two, because
clipping a word hurts more than syncing a beat into a gap.

Deps: numpy + scipy + soundfile (all already used elsewhere in DCS).
"""
from __future__ import annotations

import logging
import math

import numpy as np
import soundfile as sf

log = logging.getLogger(__name__)

# Frames below this are silence no matter what the rest of the track looks like.
_ABS_FLOOR_DB = -55.0

# Fade applied at each gate edge so hard-zeroing never clicks.
_FADE_S = 0.010

_VAD_SR = 16000


def _read_mono(path: str) -> tuple[np.ndarray, int]:
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    return data.mean(axis=1), sr


def _resample(x: np.ndarray, sr: int, target: int = _VAD_SR) -> np.ndarray:
    if sr == target:
        return x
    from math import gcd

    from scipy.signal import resample_poly

    g = gcd(sr, target)
    return resample_poly(x, target // g, sr // g).astype(np.float32)


def _rms_db(x: np.ndarray, sr: int, win_s: float, hop_s: float) -> tuple[np.ndarray, float]:
    """Short-time RMS in dBFS, plus the hop length in seconds."""
    win = max(1, int(round(win_s * sr)))
    hop = max(1, int(round(hop_s * sr)))
    if x.size < win:
        x = np.pad(x, (0, win - x.size))
    n = 1 + (x.size - win) // hop
    # Strided view -> (n, win) without copying the signal.
    frames = np.lib.stride_tricks.as_strided(
        x, shape=(n, win), strides=(x.strides[0] * hop, x.strides[0]), writeable=False
    )
    rms = np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1))
    return 20.0 * np.log10(np.maximum(rms, 1e-10)), hop / sr


def _hysteresis(db: np.ndarray, thr_hi: float, thr_lo: float) -> list[tuple[int, int]]:
    """Frame-index runs: open above thr_hi, stay open until below thr_lo."""
    runs: list[tuple[int, int]] = []
    start = None
    for i, v in enumerate(db):
        if start is None:
            if v >= thr_hi:
                start = i
        elif v < thr_lo:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(db)))
    return runs


def _merge(iv: list[tuple[float, float]], min_gap: float) -> list[tuple[float, float]]:
    if not iv:
        return []
    out = [list(iv[0])]
    for s, e in iv[1:]:
        if s - out[-1][1] <= min_gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def _silero_intervals(x: np.ndarray, sr: int) -> list[tuple[float, float]] | None:
    """Silero VAD over the stem. None means 'detector unavailable' (fall back);
    [] means 'ran fine, found no singing'."""
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
    except Exception as e:
        log.info("[vad] faster-whisper unavailable (%s) -- using the energy gate", e)
        return None
    try:
        x16 = _resample(x, sr)
        peak = float(np.max(np.abs(x16))) if x16.size else 0.0
        if peak < 1e-4:
            return []
        # Demucs stems sit well below full scale; Silero's threshold is
        # level-sensitive, so normalize before asking it anything.
        x16 = (x16 / peak) * 0.95
        opts = VadOptions(
            threshold=0.5,
            min_speech_duration_ms=200,
            min_silence_duration_ms=300,
            speech_pad_ms=120,
        )
        ts = get_speech_timestamps(x16, opts, sampling_rate=_VAD_SR)
        return [(t["start"] / _VAD_SR, t["end"] / _VAD_SR) for t in ts]
    except Exception as e:
        log.warning("[vad] Silero failed (%s) -- using the energy gate", e)
        return None


def _energy_intervals(x: np.ndarray, sr: int) -> list[tuple[float, float]]:
    """Fallback gate: short-time RMS with hysteresis.

    Thresholds are relative to the stem's own statistics, so they hold across
    quiet ballads and loud mixes alike: a robust floor (10th pct) and a robust
    peak (95th pct). The floor term rejects bleed sitting just above a
    near-silent stem; the peak term rejects loud bleed under a solo.
    """
    db, hop_s = _rms_db(x, sr, win_s=0.040, hop_s=0.010)
    audible = db[db > _ABS_FLOOR_DB]
    if audible.size < 8:
        return []

    floor = float(np.percentile(audible, 10))
    peak = float(np.percentile(audible, 95))
    if peak - floor < 6.0:
        # Flat stem: pure bleed, or a continuous a cappella. Loud => vocal.
        return [] if peak < -40.0 else [(0.0, x.size / float(sr))]

    thr_hi = max(floor + 14.0, peak - 16.0, _ABS_FLOOR_DB)
    thr_lo = thr_hi - 6.0
    return [(s * hop_s, e * hop_s) for s, e in _hysteresis(db, thr_hi, thr_lo)]


def voiced_intervals(
    vocal_wav: str,
    *,
    min_voiced: float = 0.18,
    min_gap: float = 0.30,
    pad: float = 0.10,
    max_intervals: int = 250,
) -> list[tuple[float, float]]:
    """Intervals (seconds) where the vocal stem actually carries singing.

    Returns [] when the track has no usable vocal (fully instrumental, or the
    stem is nothing but bleed), and [(0, dur)] when it is sung end to end.
    """
    x, sr = _read_mono(vocal_wav)
    dur = x.size / float(sr)
    if dur <= 0:
        return []

    iv = _silero_intervals(x, sr)
    detector = "silero"
    if iv is None:
        iv = _energy_intervals(x, sr)
        detector = "energy"
    if not iv:
        log.info("[vad] no singing detected (%s) -- instrumental track", detector)
        return []

    iv = [(s, e) for s, e in iv if e - s >= min_voiced]
    iv = _merge(iv, min_gap)
    iv = [(max(0.0, s - pad), min(dur, e + pad)) for s, e in iv]
    iv = _merge(iv, 0.0)

    # Keep the ffmpeg enable= expression a sane length: widen the merge gap until
    # the interval count fits, rather than silently dropping phrases.
    gap = min_gap
    while len(iv) > max_intervals and gap < 5.0:
        gap *= 1.6
        iv = _merge(iv, gap)
    if len(iv) > max_intervals:
        log.warning("[vad] %d intervals after merging -- capping to %d", len(iv), max_intervals)
        iv = iv[:max_intervals]

    voiced = sum(e - s for s, e in iv)
    # An instrumental stem is bleed, and peak-normalizing bleed lifts it until
    # Silero fires on a blip or two. Require a real amount of singing before we
    # believe there is any. The absolute floor dominates for short clips (so a
    # 3s talking head still syncs) and the relative term for full songs; even a
    # track carrying a single short vocal hook clears it comfortably.
    if voiced <= max(1.0, 0.02 * dur):
        log.info("[vad] only %.1fs of %.1fs voiced -- treating as instrumental", voiced, dur)
        return []
    if voiced >= 0.98 * dur:
        return [(0.0, dur)]

    log.info(
        "[vad] %s: %d vocal phrases, %.1fs of %.1fs sung (%.0f%%)",
        detector, len(iv), voiced, dur, 100.0 * voiced / dur,
    )
    return iv


def gate_audio(vocal_wav: str, intervals: list[tuple[float, float]], out_wav: str) -> bool:
    """Write vocal_wav with everything outside `intervals` hard-zeroed.

    Short raised-cosine fades at each edge keep the gate from clicking. The
    result is what MuseTalk should be driven with: real silence between phrases
    instead of Demucs bleed, so the mouth rests when nobody is singing.
    """
    try:
        data, sr = sf.read(vocal_wav, dtype="float32", always_2d=True)
        mask = np.zeros(data.shape[0], dtype=np.float32)
        for s, e in intervals:
            i0 = max(0, int(round(s * sr)))
            i1 = min(data.shape[0], int(round(e * sr)))
            if i1 > i0:
                mask[i0:i1] = 1.0

        f = max(1, int(round(_FADE_S * sr)))
        if f > 1:
            # Smooth the 0->1 and 1->0 edges of the mask in place.
            ramp = 0.5 * (1.0 - np.cos(np.linspace(0.0, math.pi, f, dtype=np.float32)))
            for idx in np.flatnonzero(np.diff(mask)):
                if mask[idx] == 0.0:                       # rising edge at idx+1
                    a, b = idx + 1, min(len(mask), idx + 1 + f)
                    mask[a:b] = np.minimum(mask[a:b], ramp[: b - a])
                else:                                      # falling edge at idx+1
                    a, b = max(0, idx + 1 - f), idx + 1
                    mask[a:b] = np.minimum(mask[a:b], ramp[::-1][f - (b - a):])

        sf.write(out_wav, data * mask[:, None], sr)
        return True
    except Exception as e:
        log.warning("[vad] gating failed (%s) -- driving with the ungated stem", e)
        return False


def enable_expr(intervals: list[tuple[float, float]]) -> str:
    """ffmpeg `enable=` expression that is true only inside `intervals`.

    `+` is arithmetic OR here: ffmpeg treats any non-zero result as enabled.
    """
    return "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in intervals)
