"""Zoom folder loop -- processes a directory of images/videos as sequential zoom jobs.

Server-side so the loop survives browser tab switches.
Heartbeat-tied: auto-stops if /api/zoom/folder-loop/status goes unpolled for 120s.
"""
import logging
import threading
import time

log = logging.getLogger("zoom.loop")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".webm", ".mkv"}
ALL_EXTS = IMAGE_EXTS | VIDEO_EXTS

_lock = threading.Lock()
_HEARTBEAT_TIMEOUT = 120.0


def _blank():
    return {
        "active": False,
        "status": "idle",
        "folder": "",
        "files": [],
        "index": 0,
        "lap": 0,
        "repeat": False,
        "succeeded": 0,
        "failed": 0,
        "errors": [],
        "current_job_id": None,
        "current_file": None,
        "last_heartbeat_at": 0.0,
    }


_state = _blank()


def status() -> dict:
    with _lock:
        _state["last_heartbeat_at"] = time.time()
        return dict(_state)


def stop() -> None:
    with _lock:
        if _state["active"]:
            _state["status"] = "stopping"


def start(folder: str, files: list, settings: dict, repeat: bool) -> dict:
    with _lock:
        if _state["active"]:
            return dict(_state)
        _state.update({
            **_blank(),
            "active": True,
            "status": "running",
            "folder": folder,
            "files": files,
            "repeat": repeat,
            "last_heartbeat_at": time.time(),
        })
    threading.Thread(target=_runner, args=(dict(settings),), daemon=True, name="zoom-loop").start()
    with _lock:
        return dict(_state)


def _runner(settings: dict) -> None:
    try:
        _run_loop(settings)
    except Exception as e:
        log.exception("[zoom-loop] Unhandled error: %s", e)
    with _lock:
        _state["active"] = False
        if _state["status"] not in ("stopped", "done", "error"):
            _state["status"] = "error"


def _run_loop(settings: dict) -> None:
    while True:
        with _lock:
            if _state["status"] in ("stopping", "stopped"):
                _state["status"] = "stopped"
                _state["active"] = False
                return
            idx      = _state["index"]
            files    = _state["files"]
            repeat   = _state["repeat"]
            last_hb  = _state["last_heartbeat_at"]

        if time.time() - last_hb > _HEARTBEAT_TIMEOUT:
            log.warning("[zoom-loop] Heartbeat timeout -- stopping")
            with _lock:
                _state["status"] = "stopped"
                _state["active"] = False
            return

        if idx >= len(files):
            if repeat:
                with _lock:
                    _state["index"] = 0
                    _state["lap"] += 1
                time.sleep(1)
                continue
            else:
                with _lock:
                    _state["status"] = "done"
                    _state["active"] = False
                return

        f = files[idx]
        with _lock:
            _state["current_file"] = f["name"]
            _state["current_job_id"] = None

        log.info("[zoom-loop] %s (%d/%d lap %d)", f["name"], idx + 1, len(files), _state["lap"] + 1)

        job_id = _submit_one(f["path"], f["name"], settings)
        if job_id:
            with _lock:
                _state["current_job_id"] = job_id
            ok, err = _wait_for_job(job_id)
            with _lock:
                if ok:
                    _state["succeeded"] += 1
                else:
                    _state["failed"] += 1
                    if err and len(_state["errors"]) < 50:
                        _state["errors"].append({"file": f["name"], "error": err})
        else:
            with _lock:
                _state["failed"] += 1
                if len(_state["errors"]) < 50:
                    _state["errors"].append({"file": f["name"], "error": "submit failed"})

        with _lock:
            _state["index"] += 1

        time.sleep(1)


def _submit_one(file_path: str, name: str, settings: dict) -> str | None:
    try:
        from app import get_job_manager
        from features.zoom.pipeline import run_zoom_prep, run_zoom_pipeline
        from core.job_manager import JOB_FUN_MULTI_VIDEO

        jm = get_job_manager()
        job_settings = dict(settings)
        timeout = job_settings.pop("_timeout_seconds", None)
        job = jm.submit_with_prep(
            JOB_FUN_MULTI_VIDEO,
            run_zoom_prep,
            run_zoom_pipeline,
            file_path,
            job_settings,
            label=f"Zoom loop: {name}",
            timeout_seconds=timeout,
        )
        if job:
            job.meta["feature"] = "zoom"
            job.meta["zoom_direction"] = settings.get("zoom_direction", "out")
            return job.id
    except Exception as e:
        log.warning("[zoom-loop] Submit failed for %s: %s", name, e)
    return None


def _wait_for_job(job_id: str) -> tuple[bool, str | None]:
    from app import get_job_manager
    jm = get_job_manager()
    deadline = time.time() + 7200
    while time.time() < deadline:
        with _lock:
            if _state["status"] in ("stopping", "stopped"):
                return False, "loop stopped"
            last_hb = _state["last_heartbeat_at"]
        if time.time() - last_hb > _HEARTBEAT_TIMEOUT:
            return False, "heartbeat timeout"
        job = jm.get(job_id)
        if job is None:
            return False, "job not found"
        if job.status == "done":
            return True, None
        if job.status in ("error", "stopped", "cancelled"):
            return False, job.error or job.message or job.status
        time.sleep(3)
    return False, "job timed out"
