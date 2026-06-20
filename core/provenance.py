"""Output provenance -- stamp every generated file with the app version + the
settings that produced it, so you can always tell which build made a given video.

Two records, both best-effort (never raise):
  - an embedded mp4 metadata tag (survives copying / sharing the file)
  - a sidecar <file>.json next to it with the full record

Usage:
    from core.provenance import stamp_video, get_app_version
    stamp_video(out_path, {"feature": "zoom-in", "n_levels": 7, ...})
"""
import json
import logging
import os
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_version_cache: str | None = None


def get_app_version() -> str:
    """Return a human-readable build id, e.g. 'v1.12-outpaint-zoom-3-g6cf592f'
    (git describe). Falls back to the short commit, then 'unknown'. Cached."""
    global _version_cache
    if _version_cache is not None:
        return _version_cache
    v = "unknown"
    try:
        r = subprocess.run(
            ["git", "-C", str(_ROOT), "describe", "--tags", "--always", "--dirty"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            v = r.stdout.strip()
    except Exception as e:
        log.debug("[provenance] version detect failed: %s", e)
    _version_cache = v
    return v


def stamp_video(path: str, fields: dict | None = None) -> None:
    """Embed {app, version, created, **fields} into the video as an mp4 comment
    tag and write a <path>.json sidecar. Best-effort; logs and returns on any error."""
    try:
        rec = {
            "app": "Drop Cat Go Studio",
            "version": get_app_version(),
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            **(fields or {}),
        }
        # sidecar JSON (always, even for non-mp4)
        try:
            Path(str(path) + ".json").write_text(
                json.dumps(rec, indent=2, default=str), encoding="utf-8")
        except Exception as e:
            log.debug("[provenance] sidecar write failed: %s", e)

        if not str(path).lower().endswith((".mp4", ".mov", ".mkv")):
            return
        if not os.path.isfile(path):
            return
        # Stream-copy remux to attach metadata (fast, no re-encode).
        comment = json.dumps(rec, default=str)[:1200]
        tmp = str(path) + ".meta.mp4"
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(path), "-c", "copy",
             "-metadata", f"comment={comment}",
             "-metadata", f"DCS_version={rec['version']}",
             "-movflags", "+faststart", tmp],
            capture_output=True, timeout=120,
        )
        if r.returncode == 0 and os.path.isfile(tmp):
            os.replace(tmp, str(path))
        else:
            try:
                os.remove(tmp)
            except OSError:
                pass
            log.debug("[provenance] metadata remux failed: %s",
                      r.stderr.decode(errors="replace")[-200:])
    except Exception as e:
        log.debug("[provenance] stamp_video failed: %s", e)
