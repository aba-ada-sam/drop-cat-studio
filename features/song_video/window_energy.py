"""Per-window energy check on the isolated vocal stem -- the mechanical rule.

Ported into DCS proper 2026-08-04 (Tier-2). Spec of record: LIPSYNC_LEDGER.md,
the 2026-08-04 "WINDOW MAP" entry as hardened by the site's reciprocal audit.
CPU only (ffmpeg volumedetect), no GPU, no model.

THE RULE, verbatim in intent: measure per-window energy on the ISOLATED stem;
any window above the floor MUST be a conditioned render no matter what any map
or VAD label says; a label-vs-energy disagreement fails LOUDLY.

WHY IT IS MECHANICAL AND NOT A HABIT. Two sibling failures, same week, same
invisibility class -- neither gave any signal until a human's ears found it:

  - Studio (2026-08-04): four windows hand-labelled "interlude" measured 55-68%
    voiced on the stem. One of them was the DENSEST-vocal window in the song.
    They had been built as unconditioned filler in every cut shipped to that
    point: 79 seconds, 38% of runtime, singing with an undriven mouth. Every
    gate verified windows against the map. Nothing verified the map against the
    song.
  - The site pipeline: silero under-detected a sung window, so it was
    conditioned on digital silence -- and the take SCORED FINE, because the
    mouth correctly matched the silence it was handed.

Both are the same shape: the render is judged against the wrong reference, so
every metric agrees it is correct. The only defence is to check the audio
itself, which is cheap, and to make disagreement noisy rather than a fallback.

WHY THE STEM AND NOT THE MIX. The slice is cut from a Demucs-isolated vocal
stem, so audible energy in a window means vocals are present almost by
definition -- the instruments are already gone. That is what makes a bare
loudness measurement a valid proxy for "someone is singing here". Run this on a
raw mix and it will call every drum fill a vocal.
"""
from __future__ import annotations

import math
import shutil
import subprocess

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

# Strict on purpose: room tone, breath and separation bleed in an isolated stem
# sit far below this, so crossing it means real vocal content. Validated as a
# real separator, not a guess -- across the production stem the distribution is
# bimodal (a vocal cluster at -28..-37 and a silence cluster at -41..-57) and
# this sits in the gap.
VOICED_FLOOR_DBFS = -40.0

# Fraction of a window that must carry vocal energy before the window counts as
# sung. The ledger's rule is "~-40 dBFS / ~20pct voiced" -- a PERCENTAGE, and
# that word is load-bearing (see the MEAN DILUTION note on measure_window).
VOICED_FRAC_FLOOR = 0.20

# Granularity the window is chopped into before measuring. Short enough that a
# single sung phrase registers inside a long window, long enough to be cheap.
PROBE_S = 0.5


class WindowLabelDisagreement(RuntimeError):
    """Raised when a window's audio contradicts its label and strict=True.

    Deliberately an exception and not a log line in strict mode: the failures
    this exists to catch are ones where every downstream metric reports success,
    so there is nothing else in the system that will ever object.
    """


def window_mean_dbfs(wav: str, t0: float, t1: float) -> float | None:
    """Mean dBFS of [t0, t1) via ffmpeg volumedetect.

    Returns None when it cannot be measured. Callers MUST treat None as "no
    information", never as "silent" -- otherwise an ffmpeg failure silently
    reopens the exact hole this module closes.
    """
    dur = max(0.05, t1 - t0)
    r = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostats",
         "-ss", f"{max(0.0, t0):.4f}", "-t", f"{dur:.4f}", "-i", str(wav),
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, timeout=120,
    )
    for line in r.stderr.decode(errors="replace").splitlines():
        if "mean_volume:" in line:
            try:
                return float(line.split("mean_volume:")[1].strip().split()[0])
            except (ValueError, IndexError):
                return None
    return None


def voiced_fraction(intervals: list[tuple[float, float]], t0: float, t1: float) -> float:
    """Fraction of [t0, t1) covered by VAD-detected voiced intervals.

    Clamped to 1.0 because intervals may OVERLAP -- two intervals covering the
    same second would otherwise sum past 100%.
    """
    dur = max(1e-6, t1 - t0)
    covered = sum(max(0.0, min(t1, e) - max(t0, s))
                  for (s, e) in intervals if e > t0 and s < t1)
    return min(1.0, covered / dur)


def measure_window(stem_wav: str, t0: float, t1: float,
                   floor_dbfs: float = VOICED_FLOOR_DBFS,
                   probe_s: float = PROBE_S) -> dict:
    """Energy profile of a window: what FRACTION of it carries vocal energy.

    MEAN DILUTION -- the reason this is not a single volumedetect over the whole
    window. A mean is not scale-invariant: the longer the window, the more
    silence dilutes the singing inside it. Measured on the real production stem
    at the real production window size (19.7s / 473f), an ~8-second sung passage
    surrounded by digital silence averages to -42.6 dBFS -- BELOW the floor --
    so a mean-based check calls it instrumental and builds it as unconditioned
    filler. That is precisely the 79-second undriven-mouth bug this module was
    written to prevent, reproduced by the prevention. Calibrated: 1s of singing
    inside a 19.7s window is missed by the mean and caught by the fraction.

    Returns peak/mean/fraction. `voiced_frac` is the number that decides.
    """
    dur = max(0.05, t1 - t0)
    n = max(1, int(math.ceil(dur / probe_s)))
    levels: list[float] = []
    for i in range(n):
        a = t0 + i * probe_s
        b = min(t1, a + probe_s)
        if b - a < 0.05:
            break
        lv = window_mean_dbfs(stem_wav, a, b)
        if lv is not None:
            levels.append(lv)
    if not levels:
        return {"voiced_frac": None, "peak_dbfs": None, "mean_dbfs": None, "probes": 0}
    above = [lv for lv in levels if lv > floor_dbfs]
    return {"voiced_frac": len(above) / len(levels),
            "peak_dbfs": max(levels),
            "mean_dbfs": sum(levels) / len(levels),
            "probes": len(levels)}


def check_window(stem_wav: str, t0: float, t1: float,
                 intervals: list[tuple[float, float]] | None = None,
                 labelled_sung: bool | None = None,
                 floor_dbfs: float = VOICED_FLOOR_DBFS,
                 frac_floor: float = VOICED_FRAC_FLOOR) -> dict:
    """Measure one window and decide whether it MUST be conditioned.

    `labelled_sung` is whatever the plan/map/arc claims about this window (None
    if nothing claims anything). `intervals` is the VAD's opinion, if available.
    Neither is trusted over the measured energy.

    Returns a dict with the measurement, the verdict, and -- when they conflict
    -- a human-readable reason. `must_condition` is the only field a caller
    needs to obey: True means render this window WITH audio conditioning,
    whatever the label said.
    """
    prof = measure_window(stem_wav, t0, t1, floor_dbfs=floor_dbfs)
    e_frac = prof["voiced_frac"]
    vad_frac = voiced_fraction(intervals, t0, t1) if intervals is not None else None

    base = {"t0": t0, "t1": t1, "mean_dbfs": prof["mean_dbfs"],
            "peak_dbfs": prof["peak_dbfs"], "energy_voiced_frac": e_frac,
            "voiced_frac": vad_frac}

    # Unmeasurable audio is NOT evidence of silence. Fail toward conditioning:
    # a conditioned render of a quiet window costs a little sync quality; an
    # unconditioned render of a sung window is the 79-second bug.
    if e_frac is None:
        return {**base, "energy_says_sung": None, "must_condition": True,
                "disagreement": ("stem energy could not be measured; conditioning "
                                 "anyway rather than assuming silence")}

    energy_says_sung = e_frac >= frac_floor

    # ONE-DIRECTIONAL, exactly as the ledger writes it: "any window above the
    # floor MUST be a conditioned render no matter what any map or VAD label
    # says." The reverse was never authorized and is dangerous -- an earlier
    # draft of this function also enforced "below floor => do NOT condition",
    # which let a measurement override a CORRECT human label and tell the
    # operator the map was stale when the map was right. Energy can only ever
    # ADD conditioning here; it can never take it away from something a human
    # or the VAD says is sung.
    must = bool(energy_says_sung or labelled_sung is True
                or (vad_frac is not None and vad_frac >= frac_floor))

    pct = f"{e_frac * 100:.0f}%"
    peak = prof["peak_dbfs"]
    disagreement = None
    if energy_says_sung and labelled_sung is False:
        disagreement = (f"window {t0:.1f}-{t1:.1f}s is LABELLED instrumental but "
                        f"{pct} of it carries vocal energy on the isolated stem "
                        f"(peak {peak:.1f} dBFS, floor {floor_dbfs:.0f}) -- the "
                        f"label is wrong, not the audio. Rendering unconditioned "
                        f"here puts an undriven mouth over singing, and every sync "
                        f"metric will still call it fine.")
    elif energy_says_sung and vad_frac is not None and vad_frac < frac_floor:
        disagreement = (f"window {t0:.1f}-{t1:.1f}s carries vocal energy over {pct} "
                        f"of its length (peak {peak:.1f} dBFS) but the VAD found "
                        f"only {vad_frac * 100:.0f}% voiced -- the detector is "
                        f"under-reading. Conditioning on the measured audio.")
    elif (not energy_says_sung) and labelled_sung is True:
        # Reported, NOT acted on: the label still wins (see must, above).
        disagreement = (f"window {t0:.1f}-{t1:.1f}s is LABELLED sung but only {pct} "
                        f"of it carries vocal energy (peak {peak:.1f} dBFS) -- "
                        f"conditioning anyway on the label, but the map and the "
                        f"song disagree here and one of them is wrong.")

    return {**base, "energy_says_sung": energy_says_sung,
            "must_condition": must, "disagreement": disagreement}


def check_plan(stem_wav: str, windows: list[dict], *, strict: bool = False,
               log=print) -> list[dict]:
    """Run check_window over a whole plan. Returns the per-window results.

    Every disagreement is logged loudly. With strict=True the first one raises
    WindowLabelDisagreement instead -- use that in batch/unattended paths, where
    there is no human watching the log and a silently mislabelled window ships.

    `windows` entries: {"t0": float, "t1": float, "labelled_sung": bool|None}.
    """
    results = []
    for w in windows:
        r = check_window(stem_wav, float(w["t0"]), float(w["t1"]),
                         intervals=w.get("intervals"),
                         labelled_sung=w.get("labelled_sung"))
        if r["disagreement"]:
            log(f"[sing] WINDOW-MAP DISAGREEMENT: {r['disagreement']}")
            if strict:
                raise WindowLabelDisagreement(r["disagreement"])
        results.append(r)

    forced = [r for r in results if r["must_condition"] and r["disagreement"]]
    if forced:
        log(f"[sing] {len(forced)} of {len(results)} window(s) will be conditioned "
            f"against their label -- the song disagreed with the plan.")
    return results
