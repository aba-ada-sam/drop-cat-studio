"""Server-side song-video batch runner.

Walks a folder of images and generates one full music video per image,
reusing the same song file and settings across all images. Mirrors the
design of features/fun_videos/folder_loop.py exactly:

* HEARTBEAT-TIED. Self-terminates when the browser stops polling
  /api/song-video/batch/status for HEARTBEAT_TIMEOUT_SEC. Closing
  the tab kills the loop within ~2 minutes. Intentional.

* AUDIO PRE-ANALYZED ONCE. The song is analyzed (BPM, beats, clip
  plan) on start, then the analysis dict is passed into every job
  so each image's generation skips re-analysis.

* CONTINUE ON FAILURE. A single image error increments `failed`
  and moves to the next image.

* IN-MEMORY ONLY. A DCS restart wipes the batch.

* ONE BATCH AT A TIME. Starting a new batch stops the previous one.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Optional

log = logging.getLogger("song_batch")

HEARTBEAT_TIMEOUT_SEC = 120
_JOB_POLL_INTERVAL_SEC = 2.0

_state_lock = threading.RLock()
_state: dict[str, Any] = {}
_runner_thread: Optional[threading.Thread] = None


def _initial_state() -> dict[str, Any]:
    return {
        "active":            False,
        "folder":            "",
        "audio_name":        "",
        "images":            [],
        "index":             0,
        "lap":               1,
        "repeat":            False,
        "settings":          {},
        "succeeded":         0,
        "failed":            0,
        "errors":            [],
        "current_job_id":    None,
        "current_image":     None,
        "status":            "idle",
        "started_at":        None,
        "updated_at":        None,
        "last_heartbeat_at": None,
    }


def _record_error(msg: str) -> None:
    errs = _state.setdefault("errors", [])
    errs.append({"at": time.time(), "msg": msg[:300]})
    if len(errs) > 50:
        _state["errors"] = errs[-50:]


def _heartbeat_stale() -> bool:
    last = _state.get("last_heartbeat_at")
    if last is None:
        return False
    return (time.time() - last) > HEARTBEAT_TIMEOUT_SEC


def _public_snapshot() -> dict[str, Any]:
    return {
        "active":          _state.get("active", False),
        "folder":          _state.get("folder", ""),
        "audio_name":      _state.get("audio_name", ""),
        "total":           len(_state.get("images", [])),
        "index":           _state.get("index", 0),
        "lap":             _state.get("lap", 1),
        "repeat":          _state.get("repeat", False),
        "succeeded":       _state.get("succeeded", 0),
        "failed":          _state.get("failed", 0),
        "errors":          list(_state.get("errors", []))[-5:],
        "current_job_id":  _state.get("current_job_id"),
        "current_image":   _state.get("current_image"),
        "status":          _state.get("status", "idle"),
        "started_at":      _state.get("started_at"),
        "heartbeat_timeout_sec": HEARTBEAT_TIMEOUT_SEC,
    }


# -- Public API ----------------------------------------------------------------

def start(
    folder: str,
    images: list[dict],
    settings: dict,
    repeat: bool,
) -> dict:
    """Start a fresh song-video batch.

    Args:
        folder:   display path (informational)
        images:   list of {path: str, name: str} -- absolute filesystem paths
        settings: template applied to every image; must include audio_path,
                  audio_duration, audio_analysis, num_clips, clip_duration,
                  model_name, and all other pipeline knobs.
        repeat:   restart at index 0 after the last image
    """
    global _runner_thread, _state

    with _state_lock:
        if _state.get("active"):
            _state["status"] = "stopping"
        old_thread = _runner_thread

    if old_thread is not None and old_thread.is_alive():
        old_thread.join(timeout=5)

    with _state_lock:
        _state = _initial_state()
        _state.update({
            "active":            True,
            "folder":            folder,
            "audio_name":        settings.get("audio_name", ""),
            "images":            list(images),
            "settings":          dict(settings),
            "repeat":            bool(repeat),
            "status":            "running",
            "started_at":        time.time(),
            "updated_at":        time.time(),
            "last_heartbeat_at": time.time(),
        })
        _runner_thread = threading.Thread(
            target=_runner, daemon=True, name="song-batch-runner",
        )
        _runner_thread.start()
        log.info("[song-batch] started: folder=%r images=%d repeat=%s",
                 folder, len(images), repeat)
        return _public_snapshot()


def stop() -> dict:
    with _state_lock:
        if _state.get("active"):
            _state["status"] = "stopping"
            _state["updated_at"] = time.time()
            log.info("[song-batch] stop requested")
        return _public_snapshot()


def status() -> dict:
    """Return snapshot; touching last_heartbeat_at keeps the runner alive."""
    with _state_lock:
        if _state.get("active"):
            _state["last_heartbeat_at"] = time.time()
        return _public_snapshot()


# -- Runner --------------------------------------------------------------------

def _submit_one(img_path: str | None, img_name: str, settings: dict):
    from app import get_job_manager
    from core.job_manager import JOB_FUN_MULTI_VIDEO
    from features.song_video.pipeline import run_song_prep, run_song_pipeline

    job_manager = get_job_manager()
    audio_name  = settings.get("audio_name", "song")
    n_clips     = settings.get("num_clips", 4)
    clip_dur    = settings.get("clip_duration", 8)
    timeout_sec = max(1800, n_clips * 300 + 900)
    label       = f"Music video: {audio_name} / {img_name[:30]}"

    return job_manager.submit_with_prep(
        JOB_FUN_MULTI_VIDEO,
        run_song_prep,
        run_song_pipeline,
        img_path or None,
        settings,
        label=label,
        timeout_seconds=timeout_sec,
    )


def _wait_for_job(job_id: str) -> tuple[bool, str]:
    from app import get_job_manager
    job_manager = get_job_manager()

    while True:
        with _state_lock:
            if not _state.get("active") or _state.get("status") in ("stopping", "stopped"):
                try:
                    job_manager.stop(job_id)
                except Exception:
                    pass
                return False, "Batch stopped"
            if _heartbeat_stale():
                try:
                    job_manager.stop(job_id)
                except Exception:
                    pass
                return False, "Browser disconnected"

        job = job_manager.get(job_id)
        if job is None:
            return False, "Job not found"
        s = job.status
        if s == "done":
            return True, ""
        if s in ("error", "stopped", "cancelled"):
            return False, (job.error or s)[:200]
        time.sleep(_JOB_POLL_INTERVAL_SEC)


def _runner() -> None:
    log.info("[song-batch] runner thread up")
    try:
        while True:
            with _state_lock:
                if not _state.get("active"):
                    break
                if _state.get("status") == "stopping":
                    _state["status"]     = "stopped"
                    _state["active"]     = False
                    _state["updated_at"] = time.time()
                    break
                if _heartbeat_stale():
                    stale_for = int(time.time() - (_state.get("last_heartbeat_at") or time.time()))
                    log.warning("[song-batch] heartbeat stale (%ds); stopping.", stale_for)
                    _record_error(f"Browser disconnected ({stale_for}s without polling); batch stopped.")
                    _state["status"]     = "stopped"
                    _state["active"]     = False
                    _state["updated_at"] = time.time()
                    break

                idx   = _state["index"]
                total = len(_state["images"])

                if idx >= total:
                    if _state.get("repeat"):
                        _state["lap"]   += 1
                        _state["index"]  = 0
                        idx = 0
                        log.info("[song-batch] lap %d -- restarting from image 0", _state["lap"])
                    else:
                        _state["status"]     = "done"
                        _state["active"]     = False
                        _state["updated_at"] = time.time()
                        log.info("[song-batch] completed: %d/%d succeeded, %d failed",
                                 _state.get("succeeded", 0), total, _state.get("failed", 0))
                        break

                img      = _state["images"][idx]
                settings = dict(_state["settings"])

            try:
                job    = _submit_one(img["path"], img["name"], settings)
                job_id = job.id
            except Exception as e:
                log.warning("[song-batch] submit failed for %s: %s", img.get("path"), e)
                with _state_lock:
                    _state["failed"]     += 1
                    _record_error(f"Submit failed for {img['name']}: {e}")
                    _state["index"]      += 1
                    _state["updated_at"]  = time.time()
                continue

            with _state_lock:
                _state["current_job_id"] = job_id
                _state["current_image"]  = img["name"]
                _state["updated_at"]     = time.time()

            ok, err = _wait_for_job(job_id)

            with _state_lock:
                if ok:
                    _state["succeeded"] += 1
                elif err not in ("Browser disconnected", "Batch stopped"):
                    _state["failed"] += 1
                    _record_error(f"{img['name']}: {err}")
                _state["index"]          = idx + 1
                _state["current_job_id"] = None
                _state["current_image"]  = None
                _state["updated_at"]     = time.time()

            time.sleep(1)

    except Exception as e:
        log.exception("[song-batch] runner crashed: %s", e)
        with _state_lock:
            _state["active"]     = False
            _state["status"]     = "error"
            _state["updated_at"] = time.time()
            _record_error(f"Runner crashed: {e}")

    log.info("[song-batch] runner exiting (status=%s, succeeded=%d, failed=%d)",
             _state.get("status"), _state.get("succeeded", 0), _state.get("failed", 0))


# Initialize at import time
_state = _initial_state()
