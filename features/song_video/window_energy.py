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

import re
import shutil
import subprocess

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

# Strict on purpose: room tone, breath and separation bleed in an isolated stem
# sit far below this, so crossing it means real vocal content.
VOICED_FLOOR_DBFS = -40.0

# Fraction of a window that must be VAD-voiced for the label "sung" to be
# uncontroversial. Below this AND above the energy floor is the disagreement
# that has to be surfaced.
VOICED_FRAC_FLOOR = 0.20


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
    """Fraction of [t0, t1) covered by VAD-detected voiced intervals."""
    dur = max(1e-6, t1 - t0)
    covered = sum(max(0.0, min(t1, e) - max(t0, s))
                  for (s, e) in intervals if e > t0 and s < t1)
    return min(1.0, covered / dur)


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
    level = window_mean_dbfs(stem_wav, t0, t1)
    frac = voiced_fraction(intervals, t0, t1) if intervals is not None else None

    # Unmeasurable audio is NOT evidence of silence. Fail toward conditioning:
    # a conditioned render of a quiet window costs a little sync quality; an
    # unconditioned render of a sung window is the 79-second bug.
    if level is None:
        return {"t0": t0, "t1": t1, "mean_dbfs": None, "voiced_frac": frac,
                "energy_says_sung": None, "must_condition": True,
                "disagreement": ("stem energy could not be measured; conditioning "
                                 "anyway rather than assuming silence")}

    energy_says_sung = level > floor_dbfs
    must = energy_says_sung

    disagreement = None
    if energy_says_sung and labelled_sung is False:
        disagreement = (f"window {t0:.1f}-{t1:.1f}s is LABELLED instrumental but the "
                        f"isolated stem measures {level:.1f} dBFS (floor "
                        f"{floor_dbfs:.0f}) -- the label is wrong, not the audio. "
                        f"Rendering unconditioned here puts an undriven mouth over "
                        f"singing, and every sync metric will still call it fine.")
    elif energy_says_sung and frac is not None and frac < frac_floor:
        disagreement = (f"window {t0:.1f}-{t1:.1f}s measures {level:.1f} dBFS on the "
                        f"stem but the VAD found only {frac * 100:.0f}% voiced "
                        f"(floor {frac_floor * 100:.0f}%) -- the detector is "
                        f"under-reading. Conditioning on the measured audio.")
    elif (not energy_says_sung) and labelled_sung is True:
        # The harmless direction, but still a map/song mismatch worth seeing.
        disagreement = (f"window {t0:.1f}-{t1:.1f}s is LABELLED sung but the stem is "
                        f"{level:.1f} dBFS (below {floor_dbfs:.0f}) -- silent "
                        f"conditioning is correct here; the map is stale.")

    return {"t0": t0, "t1": t1, "mean_dbfs": level, "voiced_frac": frac,
            "energy_says_sung": energy_says_sung, "must_condition": must,
            "disagreement": disagreement}


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
