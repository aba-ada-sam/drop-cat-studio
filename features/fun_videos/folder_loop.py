"""Server-side folder loop runtime.

Walks a directory of images and submits one full generation per image to
the existing job manager. Sits alongside the per-job pipeline; doesn't
duplicate any generation code -- just schedules submissions.

Design choices baked in here:

* HEARTBEAT-TIED TO BROWSER SESSION. The loop is server-side (so it
  survives tab throttling and per-image failures), but it self-terminates
  when the browser stops polling /api/fun/folder-loop/status for more
  than HEARTBEAT_TIMEOUT_SEC. Closing the tab kills the loop within ~1
  minute. This is intentional. Andrew explicitly does not want
  background processes that survive a closed browser.

* CONTINUE ON PER-IMAGE FAILURE. If a single image's submit or
  generation fails, the loop logs it, increments `failed`, and moves to
  the next image. Only operator-initiated stop (or heartbeat timeout)
  ends the run.

* IN-MEMORY ONLY. No state is persisted to disk. A DCS restart wipes
  the loop. Same reason as the heartbeat rule -- if you can't see the
  loop, it shouldn't be running.

* ONE LOOP AT A TIME PER DCS PROCESS. Starting a new loop while another
  is running stops the existing one first.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

log = logging.getLogger("folder_loop")

# If the browser stops polling /status for this long, runner self-terminates.
# Chrome throttles background-tab timers to once per minute, so 60s was too
# tight -- a single throttled poll cycle would kill the loop while a job was
# still running. 120s gives two throttled cycles of headroom while still
# dying within ~2 minutes of a true browser close.
HEARTBEAT_TIMEOUT_SEC = 120

# How often the runner polls a submitted job's status.
_JOB_POLL_INTERVAL_SEC = 2.0

_state_lock = threading.RLock()
_state: dict[str, Any] = {}
_runner_thread: Optional[threading.Thread] = None


def _initial_state() -> dict[str, Any]:
    return {
        "active":            False,
        "folder":            "",
        "images":            [],     # [{path, name}, ...]
        "index":             0,
        "lap":               1,
        "repeat":            False,
        "settings":          {},     # template applied to each image
        "endpoint":          "/api/fun/make-it",
        "succeeded":         0,
        "failed":            0,
        "errors":            [],     # capped ring buffer of recent errors
        "current_job_id":    None,
        "current_image":     None,
        "status":            "idle", # idle | running | stopping | stopped | done | error
        "started_at":        None,
        "updated_at":        None,
        "last_heartbeat_at": None,
    }


def _record_error(msg: str) -> None:
    errs = _state.setdefault("errors", [])
    errs.append({"at": time.time(), "msg": msg[:300]})
    # cap at 50 entries
    if len(errs) > 50:
        _state["errors"] = errs[-50:]


def _heartbeat_stale() -> bool:
    last = _state.get("last_heartbeat_at")
    if last is None:
        return False
    return (time.time() - last) > HEARTBEAT_TIMEOUT_SEC


def _public_snapshot() -> dict[str, Any]:
    """Return the user-facing slice of state (no internals)."""
    return {
        "active":         _state.get("active", False),
        "folder":         _state.get("folder", ""),
        "total":          len(_state.get("images", [])),
        "index":          _state.get("index", 0),
        "lap":            _state.get("lap", 1),
        "repeat":         _state.get("repeat", False),
        "endpoint":       _state.get("endpoint", "/api/fun/make-it"),
        "succeeded":      _state.get("succeeded", 0),
        "failed":         _state.get("failed", 0),
        "errors":         list(_state.get("errors", []))[-5:],
        "current_job_id": _state.get("current_job_id"),
        "current_image":  _state.get("current_image"),
        "status":         _state.get("status", "idle"),
        "started_at":     _state.get("started_at"),
        "heartbeat_timeout_sec": HEARTBEAT_TIMEOUT_SEC,
    }


# -- Public API ----------------------------------------------------------------

def start(folder: str, images: list[dict], settings: dict,
          endpoint: str, repeat: bool) -> dict:
    """Start a fresh folder loop.

    If an existing loop is running, stop it and wait briefly before
    starting the new one.

    Args:
        folder:   absolute path to the folder (informational; the actual
                  image paths come from `images`)
        images:   list of {"path": str, "name": str} -- absolute paths
                  the server can read directly
        settings: dict that will be passed verbatim as the per-image job
                  settings (Express request body shape, minus photo_path)
        endpoint: '/api/fun/make-it' for single-clip, '/api/fun/make-it-multi'
                  for multi-clip story mode
        repeat:   when True, restart at image 0 after the last image
    """
    global _runner_thread, _state

    with _state_lock:
        if _state.get("active"):
            _state["status"] = "stopping"
        old_thread = _runner_thread

    # Wait briefly for old runner to acknowledge stop, outside the lock
    if old_thread is not None and old_thread.is_alive():
        old_thread.join(timeout=5)

    with _state_lock:
        _state = _initial_state()
        _state.update({
            "active":            True,
            "folder":            folder,
            "images":            list(images),
            "settings":          dict(settings),
            "endpoint":          endpoint,
            "repeat":            bool(repeat),
            "status":            "running",
            "started_at":        time.time(),
            "updated_at":        time.time(),
            "last_heartbeat_at": time.time(),
        })
        _runner_thread = threading.Thread(
            target=_runner, daemon=True, name="folder-loop-runner",
        )
        _runner_thread.start()
        log.info("[folder-loop] started: folder=%r images=%d endpoint=%s repeat=%s",
                 folder, len(images), endpoint, repeat)
        return _public_snapshot()


def stop() -> dict:
    """Signal the runner to exit at the next checkpoint."""
    with _state_lock:
        if _state.get("active"):
            _state["status"] = "stopping"
            _state["updated_at"] = time.time()
            log.info("[folder-loop] stop requested by client")
        return _public_snapshot()


def status() -> dict:
    """Return a snapshot. Updates last_heartbeat_at as a side effect.

    A client polling this endpoint regularly is what keeps the loop
    alive; if calls stop for HEARTBEAT_TIMEOUT_SEC, the runner stops
    itself with a 'browser disconnected' note in the error log.
    """
    with _state_lock:
        if _state.get("active"):
            _state["last_heartbeat_at"] = time.time()
        return _public_snapshot()


# -- Runner --------------------------------------------------------------------

def _submit_one(img_path: str, img_name: str, settings: dict, endpoint: str):
    """Submit a single generation to the job manager. Returns Job or raises."""
    from app import get_job_manager
    from core.job_manager import JOB_FUN_VIDEO, JOB_FUN_MULTI_VIDEO
    from features.fun_videos.pipeline import run_prep, run_pipeline
    from features.fun_videos.multi_pipeline import run_multi_prep, run_multi_pipeline

    job_manager = get_job_manager()
    label = f"Loop: {img_name[:40]}"

    if endpoint == "/api/fun/make-it-multi":
        return job_manager.submit_with_prep(
            JOB_FUN_MULTI_VIDEO, run_multi_prep, run_multi_pipeline,
            img_path, settings, label=label,
        )
    return job_manager.submit_with_prep(
        JOB_FUN_VIDEO, run_prep, run_pipeline,
        img_path, settings, label=label,
    )


def _wait_for_job(job_id: str) -> tuple[bool, str]:
    """Poll the job manager until the job reaches a terminal status.

    Also watches the loop's own stop/heartbeat state so the runner can
    bail out of waiting if the client disconnects or hits Stop.

    Returns (success: bool, error_text: str).
    """
    from app import get_job_manager
    job_manager = get_job_manager()

    while True:
        # Outer-loop control: stop / heartbeat
        with _state_lock:
            if not _state.get("active") or _state.get("status") in ("stopping", "stopped"):
                try:
                    job_manager.stop(job_id)
                except Exception:
                    pass
                return False, "Loop stopped"
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
    """Background worker: walk images, submit each, advance."""
    log.info("[folder-loop] runner thread up")
    try:
        while True:
            # -- Per-iteration control checks --
            with _state_lock:
                if not _state.get("active"):
                    break
                if _state.get("status") == "stopping":
                    _state["status"]      = "stopped"
                    _state["active"]      = False
                    _state["updated_at"]  = time.time()
                    break
                if _heartbeat_stale():
                    stale_for = int(time.time() - (_state.get("last_heartbeat_at") or time.time()))
                    log.warning("[folder-loop] heartbeat stale (%ds without /status poll); "
                                "stopping. Browser likely closed.", stale_for)
                    _record_error(f"Browser disconnected ({stale_for}s without polling); "
                                  "loop stopped automatically.")
                    _state["status"]     = "stopped"
                    _state["active"]     = False
                    _state["updated_at"] = time.time()
                    break

                idx = _state["index"]
                total = len(_state["images"])

                # End of folder: wrap if repeat, otherwise done.
                if idx >= total:
                    if _state.get("repeat"):
                        _state["lap"] += 1
                        _state["index"] = 0
                        idx = 0
                        log.info("[folder-loop] lap %d -- restarting from image 0",
                                 _state["lap"])
                    else:
                        _state["status"]     = "done"
                        _state["active"]     = False
                        _state["updated_at"] = time.time()
                        log.info("[folder-loop] completed: %d/%d succeeded, %d failed",
                                 _state.get("succeeded", 0), total, _state.get("failed", 0))
                        break

                img = _state["images"][idx]
                settings = dict(_state["settings"])
                endpoint = _state["endpoint"]

            # -- Submit one image (outside the lock) --
            try:
                job = _submit_one(img["path"], img["name"], settings, endpoint)
                job_id = job.id
            except Exception as e:
                log.warning("[folder-loop] submit failed for %s: %s", img["path"], e)
                with _state_lock:
                    _state["failed"] += 1
                    _record_error(f"Submit failed for {img['name']}: {e}")
                    _state["index"] += 1
                    _state["updated_at"] = time.time()
                continue

            with _state_lock:
                _state["current_job_id"] = job_id
                _state["current_image"]  = img["name"]
                _state["updated_at"]     = time.time()

            # -- Wait for the job, watching for stop/heartbeat --
            ok, err = _wait_for_job(job_id)

            with _state_lock:
                if ok:
                    _state["succeeded"] += 1
                elif err in ("Browser disconnected", "Loop stopped"):
                    # Job was abandoned mid-queue, not a generation failure --
                    # don't penalise the success rate.
                    pass
                else:
                    _state["failed"] += 1
                    _record_error(f"{img['name']}: {err}")
                # If wait_for_job returned due to stop/disconnect, the outer
                # iteration's control checks will exit on the next pass --
                # we still advance the index here so a manual stop counts the
                # current image as "attempted".
                _state["index"]          = idx + 1
                _state["current_job_id"] = None
                _state["current_image"]  = None
                _state["updated_at"]     = time.time()

            # Tiny breathing room so the UI can render the new state before
            # the next submit fires.
            time.sleep(1)

    except Exception as e:
        log.exception("[folder-loop] runner crashed: %s", e)
        with _state_lock:
            _state["active"]     = False
            _state["status"]     = "error"
            _state["updated_at"] = time.time()
            _record_error(f"Runner crashed: {e}")

    log.info("[folder-loop] runner exiting (status=%s, succeeded=%d, failed=%d)",
             _state.get("status"), _state.get("succeeded", 0), _state.get("failed", 0))


# Initialize at import time
_state = _initial_state()
