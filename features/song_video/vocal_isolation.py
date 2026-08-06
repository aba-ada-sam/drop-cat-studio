"""Demucs vocal-stem isolation for the native audio-conditioning path.

RELOCATED 2026-08-05 during the MuseTalk removal (was the `_paths` /
`_separate_vocals` helpers inside features/lipsync/runner.py). Isolated-vocal
conditioning at 150Hz highpass is RATIFIED (REVIEW_FINDINGS_2026-08-05.md) and
is used exclusively by _isolate_guide_vocals in pipeline.py -- it has nothing
to do with MuseTalk's rendering itself, but it physically ran (and still
runs) via the Demucs-capable Python environment that used to live alongside
the MuseTalk install, because DCS's own Python is CPU-torch only and does not
carry demucs.

FLAGGED, not guessed at: the config keys below (`musetalk_dir` /
`musetalk_python`) are stale names now that MuseTalk itself is gone -- they
really mean "a CUDA + demucs capable venv". core/config.py's DEFAULTS entries
for them were removed by the MuseTalk-removal commit (6272910), but
core.config.get() reads straight from config.json's raw contents first (see
core/config.py:234 get()), so an existing installed config.json still
resolves them unchanged; a fresh install falls back to the hardcoded
_DEFAULT_DIR below, same as before. Left as-is rather than renamed here
because a rename touches core/config.py and the Settings UI, both outside
this lane; flagged in the MuseTalk-removal report for a follow-up decision
(new dedicated venv + neutrally-named config key, vs. keep reusing C:\\MuseTalk
purely as a demucs host).
"""
import logging
import os
import subprocess
from pathlib import Path

from core import config as cfg

log = logging.getLogger(__name__)

_DEFAULT_DIR = r"C:\MuseTalk"


def _paths():
    """Directory + python.exe of the demucs-capable venv (see module docstring
    for why this still reads the `musetalk_*` config keys)."""
    d = Path(cfg.get("musetalk_dir") or _DEFAULT_DIR)
    py = cfg.get("musetalk_python") or str(d / "venv" / "Scripts" / "python.exe")
    return d, Path(py)


def _separate_vocals(venv_py: Path, in_audio: str, out_vocals: str) -> bool:
    """Isolate the vocal stem via Demucs, run in the demucs-capable venv."""
    helper = Path(__file__).resolve().parent / "_demucs_separate.py"
    env = dict(os.environ)
    env["TORCHDYNAMO_DISABLE"] = "1"
    env["PYTHONUTF8"] = "1"
    try:
        r = subprocess.run(
            [str(venv_py), str(helper), in_audio, out_vocals],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, env=env,
        )
        if r.returncode != 0:
            log.warning("[vocal-isolation] demucs failed:\n%s", (r.stderr or "")[-800:])
        return r.returncode == 0 and os.path.isfile(out_vocals)
    except Exception as e:
        log.warning("[vocal-isolation] demucs exception: %s", e)
        return False
