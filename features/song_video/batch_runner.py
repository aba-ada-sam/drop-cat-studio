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

* PERSISTENT STATE. Batch progress is saved to disk on every state
  change and restored on DCS restart. A batch that was running when
  DCS crashed will auto-resume from where it left off when the app
  restarts and the browser reconnects.

* ONE BATCH AT A TIME. Starting a new batch stops the previous one.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("song_batch")

HEARTBEAT_TIMEOUT_SEC = 120   # 2 minutes -- batch stops if browser disconnects
_JOB_POLL_INTERVAL_SEC = 2.0

# Persist state here so a DCS restart can auto-resume an in-progress batch.
# audio_analysis blob is stripped before saving (large + can be re-run).
_STATE_FILE = Path(__file__).resolve().parent.parent.parent / "output" / "batch_state.json"

_state_lock     = threading.RLock()
_state: dict[str, Any] = {}
_runner_thread: Optional[threading.Thread] = None

# Separate lock for claiming the next image index so both worker threads
# can advance independently without stepping on each other.
_index_lock = threading.Lock()


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


def _save_state() -> None:
    """Persist current batch state to disk. Called after every meaningful state change."""
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        save = {
            k: v for k, v in _state.items()
            if k not in ("last_heartbeat_at",)   # don't save ephemeral heartbeat
        }
        # Strip audio_analysis from settings — it's large and can be re-run.
        settings = dict(save.get("settings", {}))
        settings.pop("audio_analysis", None)
        save["settings"] = settings
        _STATE_FILE.write_text(json.dumps(save, default=str), encoding="utf-8")
    except Exception as exc:
        log.warning("[song-batch] state save failed: %s", exc)


def _clear_state_file() -> None:
    try:
        _STATE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def load_saved_state() -> dict | None:
    """Return persisted batch state if a batch was running when DCS last stopped.

    Called by the API on startup. Returns None if no valid saved state.
    The caller should call start() with the saved state to resume.
    """
    try:
        if not _STATE_FILE.exists():
            return None
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        if data.get("status") == "running" and data.get("images"):
            return data
    except Exception as exc:
        log.warning("[song-batch] could not read saved state: %s", exc)
    return None


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
    # Pull per-clip progress from the currently running job so the UI can
    # show "clip 5/27" without a separate API call.
    clips_done = None
    clips_total = None
    job_id = _state.get("current_job_id")
    if job_id:
        try:
            from app import get_job_manager
            j = get_job_manager().get(job_id)
            if j and j.meta:
                clips_done  = j.meta.get("clips_done")
                clips_total = j.meta.get("clips_total")
        except Exception:
            pass

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
        "clips_done":      clips_done,
        "clips_total":     clips_total,
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
    _resume_index: int = 0,
    _resume_lap: int = 1,
    _resume_succeeded: int = 0,
    _resume_failed: int = 0,
) -> dict:
    """Start (or resume) a song-video batch.

    Args:
        folder:   display path (informational)
        images:   list of {path: str, name: str} -- absolute filesystem paths
        settings: template applied to every image
        repeat:   restart at index 0 after the last image
        _resume_*: internal resume parameters -- set by resume() only
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
            "index":             _resume_index,
            "lap":               _resume_lap,
            "succeeded":         _resume_succeeded,
            "failed":            _resume_failed,
            "started_at":        time.time(),
            "updated_at":        time.time(),
            "last_heartbeat_at": time.time(),
        })
        _save_state()
        _runner_thread = threading.Thread(
            target=_runner, daemon=True, name="song-batch-runner",
        )
        _runner_thread.start()
        log.info("[song-batch] started: folder=%r images=%d repeat=%s",
                 folder, len(images), repeat)
        return _public_snapshot()


def resume(saved: dict) -> dict:
    """Resume a batch from a saved state (called after DCS restart).

    The browser must reconnect and start polling /batch/status within
    HEARTBEAT_TIMEOUT_SEC or the runner will self-terminate as normal.
    """
    saved["last_heartbeat_at"] = time.time()
    saved["status"] = "running"
    saved["active"] = True
    # Re-analyze audio if analysis was stripped from saved settings
    if "audio_analysis" not in saved.get("settings", {}):
        audio_path = saved.get("settings", {}).get("audio_path", "")
        if audio_path and os.path.isfile(audio_path):
            try:
                from features.song_video.audio_analyzer import analyze as _analyze
                clip_dur = saved["settings"].get("clip_duration", 8)
                analysis = _analyze(audio_path, int(clip_dur))  # synchronous call
                saved["settings"]["audio_analysis"] = analysis
                log.info("[song-batch] re-analyzed audio for resume")
            except Exception as e:
                log.warning("[song-batch] audio re-analysis failed on resume: %s", e)
    return start(
        folder   = saved.get("folder", ""),
        images   = saved.get("images", []),
        settings = saved.get("settings", {}),
        repeat   = saved.get("repeat", False),
        _resume_index = saved.get("index", 0),
        _resume_lap   = saved.get("lap", 1),
        _resume_succeeded = saved.get("succeeded", 0),
        _resume_failed    = saved.get("failed", 0),
    )


def stop() -> dict:
    with _state_lock:
        if _state.get("active"):
            _state["status"] = "stopping"
            _state["updated_at"] = time.time()
            log.info("[song-batch] stop requested")
        _save_state()
        return _public_snapshot()


def status() -> dict:
    """Return snapshot; touching last_heartbeat_at keeps the runner alive."""
    with _state_lock:
        if _state.get("active"):
            _state["last_heartbeat_at"] = time.time()
        return _public_snapshot()


# -- Runner --------------------------------------------------------------------

def _satellite_available() -> bool:
    try:
        from features.fun_videos.video_generator import satellite_alive
        return satellite_alive()
    except Exception:
        return False


def _submit_one(img_path: str | None, img_name: str, settings: dict, use_satellite: bool = False):
    from app import get_job_manager
    from core.job_manager import JOB_FUN_MULTI_VIDEO, JOB_FUN_MULTI_VIDEO_SAT
    from features.song_video.pipeline import run_song_prep, run_song_pipeline
    from features.fun_videos.video_generator import WANGP_SATELLITE_URL

    job_manager = get_job_manager()
    audio_name  = settings.get("audio_name", "song")
    n_clips     = settings.get("num_clips", 4)
    timeout_sec = max(1800, n_clips * 300 + 900)

    job_settings = dict(settings)
    if use_satellite:
        job_settings["wangp_worker_url"] = WANGP_SATELLITE_URL
        label    = f"[3060] Music video: {audio_name} / {img_name[:25]}"
        job_type = JOB_FUN_MULTI_VIDEO_SAT   # not GPU-queued; runs in its own thread
    else:
        label    = f"Music video: {audio_name} / {img_name[:30]}"
        job_type = JOB_FUN_MULTI_VIDEO

    # Flag so the queue UI can show a loop icon on repeat-batch jobs
    job_settings["batch_loop"] = bool(_state.get("repeat", False))

    return job_manager.submit_with_prep(
        job_type,
        run_song_prep,
        run_song_pipeline,
        img_path or None,
        job_settings,
        label=label,
        timeout_seconds=timeout_sec,
    )


def _wait_until_gpu_phase(job_id: str) -> bool:
    """Block until a job leaves the prep/preparing state (enters GPU queue or errors).

    Returns True if the job is now in GPU phase (queued/running), False if it
    failed during prep or the batch was stopped.  Called so the next image's
    prep can start while the current job waits for the GPU.
    """
    from app import get_job_manager
    job_manager = get_job_manager()
    while True:
        with _state_lock:
            if not _state.get("active") or _state.get("status") in ("stopping", "stopped"):
                return False
        job = job_manager.get(job_id)
        if job is None:
            return False
        s = job.status
        if s in ("error", "stopped", "cancelled"):
            return False
        if s != "preparing":   # queued / running / done
            return True
        time.sleep(1)


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
    """Two-slot parallel runner.

    Spawns up to two worker threads: one for the local 5080 and (if satellite
    is reachable) one for the 3060.  Both threads share an atomic image-index
    counter so whichever GPU finishes first immediately claims the next image.
    If the satellite is unavailable at startup, only the local thread runs.
    If the satellite dies mid-batch the satellite thread exits and the local
    thread finishes alone -- no hang, no crash.
    """
    log.info("[song-batch] runner thread up")

    use_satellite = bool(_state.get("settings", {}).get("use_satellite")) \
                    and _satellite_available()

    if use_satellite:
        log.info("[song-batch] parallel mode: 5080 + 3060 satellite")
    else:
        log.info("[song-batch] single-GPU mode: 5080 only")

    def _worker(slot_name: str, use_sat: bool) -> None:
        """Worker loop for one GPU slot.  Runs until the batch is done/stopped.

        Uses a 1-ahead prep pipeline: as soon as the current job leaves the prep
        phase and enters the GPU queue, the next image's prep starts immediately.
        This overlaps LLM/audio analysis (~60s) with GPU clip generation (~20-40min),
        eliminating dead time between consecutive images.
        """
        log.info("[song-batch][%s] worker started", slot_name)

        def _claim_next() -> tuple[dict | None, dict]:
            """Claim the next image index under locks. Returns (img, settings) or (None, {})."""
            with _index_lock:
                with _state_lock:
                    idx   = _state["index"]
                    total = len(_state["images"])
                    if idx >= total:
                        if _state.get("repeat"):
                            _state["lap"]   += 1
                            _state["index"]  = 0
                            _save_state()
                            log.info("[song-batch][%s] lap %d", slot_name, _state["lap"])
                            idx = 0
                        else:
                            if _state.get("status") == "running":
                                _state["status"]     = "done"
                                _state["active"]     = False
                                _state["updated_at"] = time.time()
                                _clear_state_file()
                                log.info("[song-batch] completed: %d/%d succeeded, %d failed",
                                         _state.get("succeeded", 0), total,
                                         _state.get("failed", 0))
                            return None, {}
                    img = _state["images"][idx]
                    _state["index"] += 1
            return img, dict(_state.get("settings", {}))

        def _stop_check() -> bool:
            with _state_lock:
                return (not _state.get("active") or
                        _state.get("status") in ("stopping", "stopped") or
                        _heartbeat_stale())

        try:
            # Prime the pipeline: claim and submit the first image.
            img, settings = _claim_next()
            if img is None:
                return

            try:
                current_job = _submit_one(img["path"], img["name"], settings, use_satellite=use_sat)
            except Exception as e:
                log.warning("[song-batch][%s] submit failed: %s", slot_name, e)
                with _state_lock:
                    _state["failed"] += 1
                    _record_error(f"[{slot_name}] Submit failed for {img['name']}: {e}")
                    _state["updated_at"] = time.time()
                    _save_state()
                return

            while True:
                if _stop_check():
                    break

                with _state_lock:
                    _state["current_job_id"] = current_job.id
                    _state["current_image"]  = f"[{slot_name}] {img['name']}"
                    _state["updated_at"]     = time.time()
                    _save_state()

                # Wait for prep to finish and job to enter the GPU queue.
                # Once it does, immediately start the next image's prep so it
                # overlaps with the current image's GPU generation work.
                _wait_until_gpu_phase(current_job.id)

                next_job = None
                next_img = None
                if not _stop_check():
                    # Keep trying images until one submits successfully or we run out.
                    while True:
                        next_img, next_settings = _claim_next()
                        if next_img is None:
                            break
                        try:
                            next_job = _submit_one(next_img["path"], next_img["name"],
                                                    next_settings, use_satellite=use_sat)
                            log.debug("[song-batch][%s] started prep for next image: %s",
                                      slot_name, next_img["name"])
                            break
                        except Exception as e:
                            log.warning("[song-batch][%s] submit failed for %s, skipping: %s",
                                        slot_name, next_img["name"], e)
                            with _state_lock:
                                _state["failed"] += 1
                                _record_error(f"[{slot_name}] Submit failed for {next_img['name']}: {e}")
                                _state["updated_at"] = time.time()
                                _save_state()
                            next_img = None
                            # Try the next image instead of stopping.

                # Now wait for the current job to fully complete.
                ok, err = _wait_for_job(current_job.id)

                with _state_lock:
                    if ok:
                        _state["succeeded"] += 1
                    elif err not in ("Browser disconnected", "Batch stopped"):
                        _state["failed"] += 1
                        _record_error(f"[{slot_name}] {img['name']}: {err}")
                    _state["current_job_id"] = None
                    _state["current_image"]  = None
                    _state["updated_at"]     = time.time()
                    _save_state()

                if next_job is None:
                    break  # No more images; batch complete or stopped.

                # Advance to the next image.
                img = next_img
                current_job = next_job
                time.sleep(0.3)

        except Exception as e:
            log.exception("[song-batch][%s] worker crashed: %s", slot_name, e)
            with _state_lock:
                _record_error(f"[{slot_name}] Worker crashed: {e}")

        except Exception as e:
            log.exception("[song-batch][%s] worker crashed: %s", slot_name, e)
            with _state_lock:
                _record_error(f"[{slot_name}] Worker crashed: {e}")
                _state["updated_at"] = time.time()

        log.info("[song-batch][%s] worker exiting", slot_name)

    # ── Launch worker threads ─────────────────────────────────────────────────
    threads = [threading.Thread(target=_worker, args=("5080", False),
                                daemon=True, name="song-batch-local")]
    if use_satellite:
        threads.append(threading.Thread(target=_worker, args=("3060", True),
                                        daemon=True, name="song-batch-sat"))

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    # ── Final state cleanup ───────────────────────────────────────────────────
    with _state_lock:
        if _state.get("status") == "stopping":
            _state["status"]     = "stopped"
            _state["active"]     = False
            _state["updated_at"] = time.time()
            _save_state()
        elif _heartbeat_stale() and _state.get("active"):
            stale = int(time.time() - (_state.get("last_heartbeat_at") or time.time()))
            _record_error(f"Browser disconnected ({stale}s without polling); batch stopped.")
            _state["status"]     = "stopped"
            _state["active"]     = False
            _state["updated_at"] = time.time()
            _save_state()

    log.info("[song-batch] runner exiting (status=%s, succeeded=%d, failed=%d)",
             _state.get("status"), _state.get("succeeded", 0), _state.get("failed", 0))


# Initialize at import time
_state = _initial_state()
