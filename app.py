"""Drop Cat Go Studio -- Unified Video Production App.

Single FastAPI server combining Create Videos, Video Bridges, SD Prompts,
Image-to-Video, Video Tools, and WanGP/ACE-Step service management.
"""
# Fix double-import: when launched as `python app.py`, this file is loaded as
# __main__. Feature routes later do `from app import get_llm_router`, which
# triggers a *second* import of app.py as a fresh `app` module with its own
# empty `_g` dict -- causing the "not initialized" error. Registering ourselves
# as "app" here ensures both names share the same module object.
import sys as _sys
_sys.modules.setdefault("app", _sys.modules.get("__main__"))

# Single-instance guard -- socket lock on 127.0.0.1:7849.
# Reliable on all privilege levels; OS auto-releases when the process dies.
# Only runs when launched as the server entry point, not when imported by tests.
if _sys.modules.get("__main__") is _sys.modules.get("app"):
    import socket as _sock_mod, os as _os
    _lock_socket = _sock_mod.socket(_sock_mod.AF_INET, _sock_mod.SOCK_STREAM)
    _lock_socket.setsockopt(_sock_mod.SOL_SOCKET, _sock_mod.SO_REUSEADDR, 0)
    try:
        _lock_socket.bind(("127.0.0.1", 7849))
        _lock_socket.listen(1)
    except OSError:
        _sys.stderr.write("Drop Cat Go Studio is already running -- exiting.\n")
        _os._exit(0)

import asyncio
import json as _json_std
import logging
import logging.handlers
import os
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core import config as cfg
from core import keys, log_buffer, port_lock, session, wildcards
from core.ffmpeg_utils import ffmpeg_available
from core.hw_encoders import detect_encoders, best_encoder
from core.job_manager import JobManager
from core.llm_client import LLMClient
from core.llm_router import LLMRouter, TIER_BALANCED, TIER_FAST
from services import manager as svc

# -- Logging setup ------------------------------------------------------------

_LOG_DIR  = Path(__file__).resolve().parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_LOG_FILE = _LOG_DIR / "dropcat.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            _LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        ),
    ],
)
log = logging.getLogger("dropcat")
log_buffer.install_handler(level=logging.DEBUG)
log_buffer.capture_stdout()

# -- Globals (initialized in lifespan) ----------------------------------------
# Use a mutable dict -- dict item mutation is always visible module-wide without
# relying on 'global' inside async generators (broken in Python 3.10 asynccontextmanager).

_g: dict = {
    "job_manager":        None,
    "llm_client":         None,
    "llm_router":         None,
    "available_encoders": [],
    "gpu_vram_gb":        None,   # float GB or None if undetectable
    "boot_git_hash":      None,   # git HEAD at startup -- used for restart-needed banner
}

APP_DIR    = Path(__file__).resolve().parent
UPLOADS_DIR = APP_DIR / "uploads"
OUTPUT_DIR  = APP_DIR / "output"
STATIC_DIR  = APP_DIR / "static"
_BUILD_TS   = int(time.time())   # changes every restart; busts Chrome module-map cache

# BUG-01: create directories at module level so StaticFiles mounts succeed on
# fresh install (StaticFiles checks directory existence in __init__).
UPLOADS_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)


def _read_git_version() -> str:
    import subprocess as _sp
    try:
        date = _sp.check_output(
            ["git", "log", "-1", "--format=%cd", "--date=format:%Y-%m-%d"],
            cwd=str(APP_DIR), text=True, stderr=_sp.DEVNULL, timeout=5,
        ).strip()
        sha = _sp.check_output(
            ["git", "log", "-1", "--format=%h"],
            cwd=str(APP_DIR), text=True, stderr=_sp.DEVNULL, timeout=5,
        ).strip()
        return f"{date} . {sha}" if date and sha else "unknown"
    except Exception:
        return "unknown"


APP_VERSION = _read_git_version()

# Written by /api/jobs/save-and-restart; read at next startup to auto-restore queue.
PLANNED_RESTART_MARKER = APP_DIR / ".dcs-planned-restart"


# -- Lifespan -----------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Drop Cat Go Studio starting up...")

    # Background git pull -- keeps the app current without manual steps.
    # Runs in a daemon thread so startup is never delayed by network.
    def _bg_pull():
        import subprocess as _sp, pathlib as _pl
        repo = _pl.Path(__file__).parent
        if not (_repo := repo / ".git").exists():
            return
        try:
            r = _sp.run(["git", "-C", str(repo), "pull", "--ff-only"],
                        capture_output=True, timeout=30)
            out = (r.stdout + r.stderr).decode(errors="replace").strip()
            if r.returncode == 0:
                if "Already up to date" not in out:
                    log.info("[startup] git pull: %s", out)
            else:
                log.debug("[startup] git pull skipped: %s", out)
        except Exception as _e:
            log.debug("[startup] git pull error: %s", _e)
    import threading as _threading
    _threading.Thread(target=_bg_pull, daemon=True, name="git-pull").start()

    # Record git HEAD at boot so the UI can show a "restart needed" banner
    # when new commits have landed since this process started.
    try:
        import subprocess as _sp2
        _r = _sp2.run(["git", "-C", str(APP_DIR), "rev-parse", "HEAD"],
                      capture_output=True, text=True, timeout=5)
        _g["boot_git_hash"] = _r.stdout.strip() if _r.returncode == 0 else None
    except Exception:
        pass

    # Ensure runtime directories exist
    try:
        UPLOADS_DIR.mkdir(exist_ok=True)
        OUTPUT_DIR.mkdir(exist_ok=True)
    except Exception as _e:
        log.warning("[startup] could not create runtime dirs (non-fatal): %s", _e)

    # On a PLANNED restart (user clicked "Save & Restart") auto-restore the queue.
    # On any other startup (fresh launch, crash) delete stale queue_save.json --
    # zombie jobs from old sessions should not pollute a new session.
    _planned_restart = PLANNED_RESTART_MARKER.exists()
    if _planned_restart:
        PLANNED_RESTART_MARKER.unlink(missing_ok=True)
        log.info("[startup] planned restart -- queue will be auto-restored")
    else:
        from core.job_manager import QUEUE_SAVE_FILE as _QSF
        if _QSF.exists():
            _QSF.unlink(missing_ok=True)
            log.info("[startup] deleted stale queue_save.json from previous session")

    # Migrate config from old apps on first run
    try:
        cfg.migrate_from_old_apps()
    except Exception as _e:
        log.warning("[startup] config migration failed (non-fatal): %s", _e)

    # Initialize job manager -- critical, re-raise on failure
    _g["job_manager"] = JobManager()

    # Auto-restore queue if this was a planned restart
    if _planned_restart:
        try:
            _g["job_manager"].restore_queue(_build_restore_registry(_g["job_manager"]))
            log.info("[startup] queue auto-restored from planned restart")
        except Exception as _e:
            log.warning("[startup] queue auto-restore failed (non-fatal): %s", _e)

    # Initialize LLM client + router
    try:
        _g["llm_client"] = LLMClient(
            host=cfg.get("ollama_host") or "http://localhost:11434",
            fast_model=cfg.get("ollama_fast_model") or "qwen3-vl:8b",
            balanced_model=cfg.get("ollama_balanced_model") or "qwen3-vl:8b",
            power_model=cfg.get("ollama_power_model") or "qwen3-vl:30b",
            vision_model=cfg.get("ollama_vision_model") or "qwen3-vl:8b",
        )
        _g["llm_router"] = LLMRouter(_g["llm_client"])
    except Exception as _e:
        log.warning("[startup] LLM init failed (non-fatal, AI features disabled): %s", _e)

    # Auto-seed Anthropic key from credentials file if not already saved
    try:
        if not cfg.get("anthropic_key") and keys.get_key("anthropic"):
            cfg.save({"anthropic_key": keys.get_key("anthropic")})
            log.info("Auto-loaded Anthropic key from credentials file")
    except Exception as _e:
        log.warning("[startup] key auto-seed failed (non-fatal): %s", _e)

    # Detect GPU encoders
    try:
        _g["available_encoders"] = detect_encoders()
        if _g["available_encoders"]:
            hw = [e[1] for e in _g["available_encoders"] if e[2]]
            log.info("Encoders: %s", ", ".join(hw) if hw else "CPU only")
    except Exception as _e:
        log.warning("[startup] encoder detection failed (non-fatal): %s", _e)
        _g["available_encoders"] = []

    # Detect GPU VRAM via nvidia-smi (no torch import needed in main process)
    try:
        import subprocess as _sp2
        def _detect_vram():
            r = _sp2.run(
                ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            return r
        _r = await asyncio.to_thread(_detect_vram)
        if _r.returncode == 0:
            _mb = int(_r.stdout.strip().splitlines()[0].strip())
            _g["gpu_vram_gb"] = round(_mb / 1024, 1)
            log.info("GPU VRAM detected: %.1f GB", _g["gpu_vram_gb"])
    except Exception as _e:
        log.debug("[startup] VRAM detection failed (non-fatal): %s", _e)

    # Synchronous: evict any orphan WanGP / ACE-Step workers from a prior DCS
    # session BEFORE we accept user requests. Without this, clicking Create on
    # the new app would queue work behind the dying old worker, making restarts
    # feel like the program never closed.
    try:
        svc.kill_orphans_at_startup()
    except Exception as _e:
        log.warning("[startup] orphan eviction failed (non-fatal): %s", _e)

    # Background: detect current state then start any stopped workers
    threading.Thread(target=svc.startup_all, daemon=True).start()

    # Periodic job cleanup -- purge terminal jobs older than 15 minutes
    def _cleanup_jobs():
        import time as _time
        while True:
            _time.sleep(300)
            jm = _g.get("job_manager")
            if jm:
                jm.cleanup(max_age_hours=0.25)
    threading.Thread(target=_cleanup_jobs, daemon=True).start()


    _port = _g.get("port", 7860)
    log.info("Drop Cat Go Studio ready on http://127.0.0.1:%d", _port)

    yield

    # Shutdown
    log.info("Shutting down...")
    svc.shutdown_all()
    port_lock.clear_port_file()


# -- App ----------------------------------------------------------------------

app = FastAPI(title="Drop Cat Go Studio", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    """Prevent browsers from caching JS/CSS/HTML so stale files never break the app."""
    response = await call_next(request)
    path = str(request.url.path)
    if (path == "/" or path.startswith("/static/js/") or path.startswith("/static/css/")):
        response.headers["Cache-Control"] = "no-store"
    return response

# Mount static files and media directories (before routes for correct priority)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
# DCS_SUBAPP_STATIC: when a sub-app (DCMVS) wants its own branding assets
# alongside the shared /static mount, it sets this env to its static dir and
# we expose it at /subapp/* without disturbing /static/*.
_subapp_static = os.environ.get("DCS_SUBAPP_STATIC")
if _subapp_static and Path(_subapp_static).is_dir():
    app.mount("/subapp", StaticFiles(directory=_subapp_static), name="subapp")
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")
# NOTE: /output is served via a route below, not StaticFiles, because Starlette's
# StaticFiles path joining on Windows breaks for nested subdirectories.


# -- Global accessors (use these in features instead of direct import) ---------

def get_llm_router():
    """Return the initialized LLMRouter."""
    r = _g["llm_router"]
    if r is None:
        raise RuntimeError("LLM router not initialized -- app not fully started")
    return r


def get_job_manager():
    """Return the initialized JobManager."""
    jm = _g["job_manager"]
    if jm is None:
        raise RuntimeError("Job manager not initialized -- app not fully started")
    return jm


# -- Global routes ------------------------------------------------------------

_NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}

@app.get("/")
async def index():
    # DCS_INDEX_HTML lets a sub-app (DCMVS etc.) substitute its own branded
    # index without forking DCS -- the rest of the server is unchanged.
    _override = os.environ.get("DCS_INDEX_HTML")
    index_path = Path(_override) if _override else (STATIC_DIR / "index.html")
    if index_path.exists():
        html = index_path.read_text(encoding="utf-8")
        # Stamp app.js URL with server-start time so Chrome's ES module map
        # sees a new URL on every restart and never serves stale JS.
        html = html.replace('src="/static/js/app.js?', f'src="/static/js/app.js?b={_BUILD_TS}&')
        return HTMLResponse(content=html, headers=_NO_CACHE)
    return JSONResponse({"status": "Drop Cat Go Studio is running", "ui": "not built yet"})


@app.get("/static/js/app.js")
async def serve_app_js():
    """Serve app.js with no-cache so the browser always picks up new code after restart."""
    return FileResponse(str(STATIC_DIR / "js" / "app.js"), headers=_NO_CACHE)


@app.get("/api/version")
async def get_version():
    return {"version": APP_VERSION}


@app.get("/manifest.json")
async def serve_manifest():
    return FileResponse(str(STATIC_DIR / "manifest.json"), media_type="application/manifest+json")


@app.get("/sw.js")
async def serve_sw():
    return FileResponse(str(STATIC_DIR / "sw.js"), media_type="application/javascript")


# -- Config -------------------------------------------------------------------

@app.get("/api/config")
async def get_config():
    return cfg.load()


@app.post("/api/config")
async def update_config(request: Request):
    body = await request.json()
    # Only accept known keys
    valid = {k: v for k, v in body.items() if k in cfg.DEFAULTS}
    if valid:
        cfg.save(valid)
    return cfg.load()


@app.post("/api/config/validate-wangp")
async def validate_wangp(request: Request):
    body = await request.json()
    ok, msg = cfg.validate_wan2gp(body.get("path", ""))
    return {"ok": ok, "message": msg}


@app.post("/api/config/validate-acestep")
async def validate_acestep(request: Request):
    body = await request.json()
    ok, msg = cfg.validate_acestep(body.get("path", ""))
    return {"ok": ok, "message": msg}


# -- Ollama config -------------------------------------------------------------

@app.get("/api/ollama/models")
async def ollama_models():
    """Return list of Ollama model names installed locally."""
    return {"models": keys.get_ollama_models()}


@app.post("/api/ollama/config")
async def save_ollama_config(request: Request):
    """Save Ollama host and model preferences, hot-reload client."""
    body = await request.json()
    updates = {}
    for key in ("ollama_host", "ollama_fast_model", "ollama_balanced_model", "ollama_power_model", "ollama_vision_model"):
        if key in body:
            updates[key] = body[key]
    if updates:
        cfg.save(updates)
        if _g["llm_client"]:
            _g["llm_client"].update_config(
                host=updates.get("ollama_host", ""),
                fast_model=updates.get("ollama_fast_model", ""),
                balanced_model=updates.get("ollama_balanced_model", ""),
                power_model=updates.get("ollama_power_model", ""),
                vision_model=updates.get("ollama_vision_model", ""),
            )
    return keys.status()


# Legacy key status endpoint (returns Ollama status now)
@app.get("/api/keys/status")
async def keys_status():
    return keys.status()


# -- LLM provider config -------------------------------------------------------

@app.get("/api/llm/config")
async def get_llm_config():
    """Return current LLM provider and whether keys are set."""
    provider = cfg.get("llm_provider") or "auto"
    anthropic_key = keys.get_key("anthropic")
    openai_key = keys.get_key("openai")
    if provider == "auto":
        if anthropic_key:   effective = "anthropic"
        elif openai_key:    effective = "openai"
        else:               effective = "ollama"
    else:
        effective = provider
    return {
        "provider": provider,
        "effective_provider": effective,
        "anthropic_key_set": bool(anthropic_key),
        "openai_key_set": bool(openai_key),
        "anthropic_key_hint": f"...{anthropic_key[-4:]}" if len(anthropic_key) > 4 else ("set" if anthropic_key else ""),
        "openai_key_hint":    f"...{openai_key[-4:]}" if len(openai_key) > 4 else ("set" if openai_key else ""),
    }


@app.post("/api/llm/config")
async def save_llm_config(request: Request):
    """Save LLM provider and API keys."""
    body = await request.json()
    updates = {}
    if "provider" in body:
        updates["llm_provider"] = body["provider"]
    # Only save keys if non-empty (don't overwrite with empty string)
    if body.get("anthropic_key"):
        updates["anthropic_key"] = body["anthropic_key"]
    if body.get("openai_key"):
        updates["openai_key"] = body["openai_key"]
    if updates:
        cfg.save(updates)
    return await get_llm_config()


# -- Services -----------------------------------------------------------------

@app.get("/api/services")
async def services_status():
    return JSONResponse(content=svc.get_status(), headers={"Cache-Control": "no-store"})


@app.get("/api/satellite/status")
async def satellite_status():
    """Ping the 3060 relay and return its service status."""
    host = (cfg.get("satellite_host") or "").strip()
    if not host:
        return JSONResponse({"connected": False, "host": "", "services": {}})

    def _check():
        import urllib.request as _ur
        import time as _t
        base = f"http://{host}:9999"
        t0 = _t.time()
        with _ur.urlopen(f"{base}/ping", timeout=3) as r:
            ping = _json_std.loads(r.read())
        if not ping.get("ok"):
            raise ValueError("bad ping")
        latency = int((_t.time() - t0) * 1000)
        with _ur.urlopen(f"{base}/services", timeout=5) as r:
            services = _json_std.loads(r.read())
        return {"connected": True, "host": host, "latency_ms": latency, "services": services}

    try:
        result = await asyncio.to_thread(_check)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"connected": False, "host": host, "error": str(e), "services": {}})


@app.get("/api/gpu/status")
async def gpu_status():
    """Which service currently owns the GPU + recent eviction history."""
    from core.gpu_orchestrator import gpu
    return JSONResponse(content=gpu.status(), headers={"Cache-Control": "no-store"})


@app.post("/api/gpu/release")
async def gpu_release_all():
    """Force-evict every GPU service. Frees VRAM for other applications."""
    from core.gpu_orchestrator import gpu
    gpu.release_all()
    return {"ok": True, "current": gpu.current}


@app.post("/api/services/start/{name}")
async def start_service(name: str):
    # GPU-using services MUST go through the orchestrator so the current
    # VRAM holder is evicted before the new service loads its model on top.
    # Without this, clicking 'Start Forge' while WanGP is in VRAM causes
    # two CUDA contexts to collide and crash app.py (observed 2026-05-11).
    if name not in ("wangp", "acestep", "forge", "ollama"):
        return JSONResponse({"error": f"Unknown service: {name}"}, 404)

    def _safe_start():
        try:
            from core.gpu_orchestrator import gpu
            gpu.acquire(name, reason="manual service start from UI")
        except Exception as e:
            log.error("[services] start %s via orchestrator failed: %s", name, e)
            # Fall back to direct start so user isn't blocked if orchestrator hits a bug
            starters = {
                "wangp": svc.start_wangp_worker,
                "acestep": svc.start_acestep,
                "forge": svc.start_forge,
                "ollama": svc.start_ollama,
            }
            try: starters[name]()
            except Exception as e2: log.error("[services] direct fallback also failed: %s", e2)

    threading.Thread(target=_safe_start, daemon=True).start()
    return {"ok": True, "message": f"Starting {name}..."}


@app.post("/api/services/stop/{name}")
async def stop_service_route(name: str):
    ok, err = svc.stop_service(name)
    return {"ok": ok, "error": err}


@app.post("/api/services/restart/{name}")
async def restart_service_route(name: str):
    threading.Thread(target=svc.restart_service, args=(name,), daemon=True).start()
    return {"ok": True, "message": f"Restarting {name}..."}


@app.post("/api/app/restart")
async def restart_app():
    """Gracefully exit app.py so the manager watchdog restarts it with fresh code.

    Useful after deploying Python code changes without touching the manager process.
    Returns immediately -- the server will be unreachable for ~10 seconds then come back.
    """
    import signal, os
    log.info("App restart requested via /api/app/restart -- exiting for watchdog respawn")
    def _do_exit():
        import time; time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_do_exit, daemon=True).start()
    return {"ok": True, "message": "Restarting -- reconnect in ~10 seconds"}


# -- Logs ---------------------------------------------------------------------

@app.get("/api/logs")
async def get_logs(since: int = 0):
    if since:
        return {"logs": log_buffer.get_since(since)}
    return {"logs": log_buffer.get_recent()}


@app.post("/api/logs/client")
async def client_log(request: Request):
    """Receive browser-side errors and write them to the server log."""
    try:
        body = await request.json()
        msg  = body.get("message", "unknown client error")
        src  = body.get("source", "")
        line = body.get("lineno", "")
        log.error("CLIENT JS: %s  (%s:%s)", msg, src, line)
    except Exception as e:
        log.warning("client_log endpoint failed: %s", e)
    return {"ok": True}


@app.get("/api/logs/file")
async def get_log_file(lines: int = 200):
    """Return the last N lines from the persistent log file as plain text."""
    from fastapi.responses import PlainTextResponse
    if not _LOG_FILE.exists():
        return PlainTextResponse("Log file not found yet -- restart the app.\n")
    try:
        all_lines = _LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(all_lines[-lines:])
        return PlainTextResponse(tail + "\n")
    except Exception as e:
        return PlainTextResponse(f"Error reading log file: {e}\n")


# -- Jobs ---------------------------------------------------------------------

@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    if _g["job_manager"] is None:
        return JSONResponse({"error": "Not ready"}, 503)
    info = _g["job_manager"].get_job_info(job_id)
    if info is None:
        return JSONResponse({"error": "Job not found"}, 404)
    return info


@app.post("/api/jobs/{job_id}/stop")
async def stop_job(job_id: str):
    if _g["job_manager"] is None:
        return JSONResponse({"error": "Not ready"}, 503)
    found = _g["job_manager"].stop(job_id)
    return {"ok": found}


@app.delete("/api/jobs/{job_id}")
async def dismiss_job(job_id: str):
    """Remove a completed/failed job from the queue entirely."""
    if _g["job_manager"] is None:
        return JSONResponse({"error": "Not ready"}, 503)
    removed = _g["job_manager"].dismiss(job_id)
    return {"ok": removed}


@app.delete("/api/jobs")
async def dismiss_all_finished():
    """Remove all completed/failed/cancelled jobs from the queue."""
    if _g["job_manager"] is None:
        return JSONResponse({"error": "Not ready"}, 503)
    count = _g["job_manager"].dismiss_all_finished()
    return {"ok": True, "dismissed": count}


@app.post("/api/jobs/pause")
async def pause_queue():
    if _g["job_manager"] is None:
        return JSONResponse({"error": "Not ready"}, 503)
    _g["job_manager"].pause()
    return {"ok": True, "paused": True}


@app.post("/api/jobs/resume")
async def resume_queue():
    if _g["job_manager"] is None:
        return JSONResponse({"error": "Not ready"}, 503)
    _g["job_manager"].resume()
    return {"ok": True, "paused": False}


@app.post("/api/jobs/cancel-queued")
async def cancel_all_queued():
    if _g["job_manager"] is None:
        return JSONResponse({"error": "Not ready"}, 503)
    count = _g["job_manager"].cancel_all_queued()
    return {"ok": True, "cancelled": count}


@app.get("/api/jobs/save-queue")
async def get_save_queue_info():
    """Return info about any previously saved queue file."""
    from core.job_manager import QUEUE_SAVE_FILE
    if not QUEUE_SAVE_FILE.exists():
        return {"has_save": False, "count": 0, "saved_at": None}
    try:
        import json as _json
        data = _json.loads(QUEUE_SAVE_FILE.read_text(encoding="utf-8"))
        return {"has_save": True, "count": len(data.get("jobs", [])), "saved_at": data.get("saved_at")}
    except Exception:
        return {"has_save": False, "count": 0, "saved_at": None}


@app.post("/api/jobs/save-queue")
async def save_queue():
    """Serialize all waiting jobs to queue_save.json."""
    import asyncio as _asyncio
    if _g["job_manager"] is None:
        return JSONResponse({"error": "Not ready"}, 503)
    try:
        count = await _asyncio.wait_for(
            _asyncio.to_thread(_g["job_manager"].save_queue),
            timeout=10.0,
        )
        return {"ok": True, "saved": count}
    except _asyncio.TimeoutError:
        log.error("[save-queue] Timed out waiting for job_manager lock")
        return JSONResponse({"error": "Save timed out -- a GPU job may be holding the queue lock"}, 503)
    except Exception as e:
        log.exception("[save-queue] Save failed: %s", e)
        return JSONResponse({"error": str(e)}, 500)


def _build_restore_registry(jm):
    """Build the feature->handler registry used by both auto-restore and the manual endpoint."""
    from features.song_video.pipeline import run_song_prep, run_song_pipeline
    from features.fun_videos.pipeline import run_prep, run_pipeline
    from features.fun_videos.multi_pipeline import run_multi_prep, run_multi_pipeline
    from features.video_bridges.routes import _bridges_worker
    from features.zoom.pipeline import run_zoom_prep, run_zoom_pipeline
    from core.job_manager import JOB_FUN_VIDEO, JOB_FUN_MULTI_VIDEO, JOB_BRIDGE

    def _make_fun_video(args, label, timeout_seconds):
        photo_path = args[0] if args else None
        settings   = dict(args[1]) if len(args) > 1 else {}
        return jm.submit_with_prep(JOB_FUN_VIDEO, run_prep, run_pipeline,
                                   photo_path, settings, label=label)

    def _make_fun_multi(args, label, timeout_seconds):
        photo_path = args[0] if args else None
        settings   = dict(args[1]) if len(args) > 1 else {}
        return jm.submit_with_prep(JOB_FUN_MULTI_VIDEO, run_multi_prep, run_multi_pipeline,
                                   photo_path, settings, label=label)

    def _make_song_video(args, label, timeout_seconds):
        photo_path = args[0] if args else None
        settings   = dict(args[1]) if len(args) > 1 else {}
        n_clips    = int(settings.get("num_clips", 10))
        timeout    = timeout_seconds or max(1800, n_clips * 300 + 900)
        return jm.submit_with_prep(JOB_FUN_MULTI_VIDEO, run_song_prep, run_song_pipeline,
                                   photo_path, settings, label=label, timeout_seconds=timeout)

    def _make_bridge(args, label, timeout_seconds):
        items    = list(args[0]) if args else []
        settings = dict(args[1]) if len(args) > 1 else {}
        return jm.submit(JOB_BRIDGE, _bridges_worker, items, settings, label=label)

    def _make_zoom(args, label, timeout_seconds):
        source_path = args[0] if args else None
        settings    = dict(args[1]) if len(args) > 1 else {}
        job = jm.submit_with_prep(JOB_FUN_MULTI_VIDEO, run_zoom_prep, run_zoom_pipeline,
                                  source_path, settings, label=label,
                                  timeout_seconds=timeout_seconds)
        if job:
            job.meta["feature"] = "zoom"
            job.meta["zoom_direction"] = settings.get("zoom_direction", "out")
        return job

    return {
        "fun_video":       _make_fun_video,
        "fun_multi_video": _make_fun_multi,
        "song_video":      _make_song_video,
        "bridge":          _make_bridge,
        "zoom":            _make_zoom,
    }


@app.post("/api/jobs/restore-queue")
async def restore_queue():
    """Re-submit jobs from queue_save.json."""
    import asyncio as _asyncio
    if _g["job_manager"] is None:
        return JSONResponse({"error": "Not ready"}, 503)
    jm = _g["job_manager"]
    restored, failed = await _asyncio.to_thread(jm.restore_queue, _build_restore_registry(jm))
    return {"ok": True, "restored": restored, "failed": failed}


@app.post("/api/jobs/save-and-restart")
async def save_and_restart(background_tasks: BackgroundTasks):
    """Save the current queue then restart app.py via the manager watchdog.

    BackgroundTasks guarantees the response is fully sent before the task
    runs -- a bare threading.Thread with a short sleep races with uvicorn's
    response flush and loses under load, producing 'Failed to fetch'.
    """
    import asyncio as _asyncio, signal
    if _g["job_manager"] is None:
        return JSONResponse({"error": "Not ready"}, 503)
    jm = _g["job_manager"]
    try:
        count = await _asyncio.wait_for(
            _asyncio.to_thread(jm.save_queue),
            timeout=10.0,
        )
    except _asyncio.TimeoutError:
        log.error("[save-restart] save_queue timed out -- restarting anyway without save")
        count = 0
    PLANNED_RESTART_MARKER.write_text("planned")
    log.info("Save-and-restart: saved %d jobs -- triggering watchdog restart", count)

    async def _scheduled_exit():
        await _asyncio.sleep(0.3)
        os.kill(os.getpid(), signal.SIGTERM)

    background_tasks.add_task(_scheduled_exit)
    return {"ok": True, "saved": count}


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str):
    if _g["job_manager"] is None:
        return JSONResponse({"error": "Not ready"}, 503)
    new_job = _g["job_manager"].retry(job_id)
    if new_job is None:
        return JSONResponse({"error": "Cannot retry this job"}, 400)
    return {"ok": True, "job_id": new_job.id}


@app.post("/api/jobs/{job_id}/promote")
async def promote_job(job_id: str):
    if _g["job_manager"] is None:
        return JSONResponse({"error": "Not ready"}, 503)
    ok = _g["job_manager"].promote(job_id)
    return {"ok": ok}


@app.get("/api/jobs")
async def list_jobs():
    if _g["job_manager"] is None:
        return JSONResponse({"error": "Not ready"}, 503)
    return _g["job_manager"].queue_status()


@app.post("/api/browse-folder")
async def browse_folder():
    """Open a native OS folder-picker dialog and return the selected path."""
    import asyncio as _asyncio

    def _pick():
        try:
            import tkinter as _tk
            from tkinter import filedialog as _fd
            root = _tk.Tk()
            root.withdraw()
            root.wm_attributes("-topmost", True)
            path = _fd.askdirectory(title="Select a folder of photos")
            root.destroy()
            return path or None
        except Exception as exc:
            return {"error": str(exc)}

    result = await _asyncio.to_thread(_pick)
    if isinstance(result, dict):
        return JSONResponse(result, 500)
    return {"path": result}


_THUMBNAIL_VIDEO_EXT  = {'.mp4', '.webm', '.mov', '.avi', '.mkv'}
_THUMBNAIL_NO_SUPPORT = {'.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a'}

@app.get("/api/thumbnail")
async def get_thumbnail(path: str, size: int = 120):
    """Serve a scaled-down thumbnail of an image or video file.

    For images: PIL thumbnail. For videos: ffmpeg extracts a midpoint frame,
    then PIL scales it. The queue modal uses this to show clip-by-clip
    progress as a row of thumbnails while a multi-clip job runs.
    """
    import io
    import subprocess
    import tempfile
    from pathlib import Path as _Path
    from fastapi.responses import Response as _Resp
    try:
        from PIL import Image as _Img
        # Accept URL-style paths like /output/... or /uploads/... in addition to
        # absolute filesystem paths.  Resolve them relative to the app root.
        _url_prefixes = ('/output/', '/uploads/', '/static/')
        if any(path.startswith(pfx) for pfx in _url_prefixes):
            path = str(_Path(__file__).resolve().parent / path.lstrip('/'))
        p = _Path(path)
        if not p.is_file():
            return JSONResponse({"error": "Not found"}, status_code=404)
        suf = p.suffix.lower()
        if suf in _THUMBNAIL_NO_SUPPORT:
            return JSONResponse({"error": "No thumbnail for this file type"}, status_code=415)

        if suf in _THUMBNAIL_VIDEO_EXT:
            from core.ffmpeg_utils import probe_duration
            dur = probe_duration(str(p)) or 0.0
            seek = max(0.0, min(dur * 0.5, dur - 0.1))
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                r = subprocess.run(
                    ["ffmpeg", "-y", "-ss", f"{seek:.3f}", "-i", str(p),
                     "-frames:v", "1", "-q:v", "3", tmp_path],
                    capture_output=True, timeout=10,
                )
                if r.returncode != 0 or not _Path(tmp_path).is_file():
                    return JSONResponse({"error": "ffmpeg frame extraction failed"}, status_code=500)
                with _Img.open(tmp_path) as img:
                    img.thumbnail((size * 2, size * 2))
                    buf = io.BytesIO()
                    img.convert("RGB").save(buf, format="JPEG", quality=75)
            finally:
                try: os.remove(tmp_path)
                except OSError: pass
            return _Resp(content=buf.getvalue(), media_type="image/jpeg")

        with _Img.open(p) as img:
            img.thumbnail((size * 2, size * 2))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=75)
        return _Resp(content=buf.getvalue(), media_type="image/jpeg")
    except Exception as e:
        log.warning("Thumbnail generation failed for %s: %s", path, e)
        return JSONResponse({"error": "Could not generate thumbnail"}, status_code=500)


@app.get("/output/{path:path}")
async def serve_output_file(path: str):
    """Serve generated output files (videos, images) from nested subdirectories.

    Uses FileResponse instead of StaticFiles because Starlette's StaticFiles
    path joining on Windows incorrectly resolves paths for nested directories.
    """
    file_path = (OUTPUT_DIR / path).resolve()
    # Guard against directory traversal
    if not str(file_path).startswith(str(OUTPUT_DIR.resolve())):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    if not file_path.is_file():
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse(str(file_path))


def _find_vlc() -> str | None:
    """Locate VLC executable. FLW-03: use PATH first, then common install locations."""
    import shutil
    found = shutil.which("vlc") or shutil.which("vlc.exe")
    if found:
        return found
    candidates = [
        r"C:\Program Files\VideoLAN\VLC\vlc.exe",
        r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


@app.post("/api/reveal")
async def reveal_in_explorer(request: Request):
    """Open file in Explorer (reveal) or VLC depending on action."""
    import subprocess as _sp
    body = await request.json()
    path = body.get("path", "")
    action = body.get("action", "explorer")  # "explorer" | "vlc"
    file_path = Path(path).resolve() if os.path.isabs(path) else (OUTPUT_DIR / path).resolve()
    if not str(file_path).startswith(str(OUTPUT_DIR.resolve())):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    if not file_path.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)
    vlc = _find_vlc()
    if action == "vlc" and vlc:
        _sp.Popen([vlc, str(file_path)])
    else:
        _sp.Popen(["explorer", "/select,", str(file_path)])
    return {"ok": True}


@app.post("/api/tools/extract-frame")
async def extract_frame_endpoint(request: Request):
    """Extract a single frame from a video and save it as a JPEG image.

    Body: { path: str, position: float (0.0=first, 1.0=last, default 0.5) }
    Returns: { path: absolute_path, url: /output/frames/filename.jpg }
    """
    import base64, time
    from core.ffmpeg_utils import extract_frame_b64

    body = await request.json()
    path = body.get("path", "")
    position = float(body.get("position", 0.5))

    if not path:
        raise HTTPException(status_code=400, detail="path required")

    vid_path = Path(path) if os.path.isabs(path) else OUTPUT_DIR / path
    if not vid_path.exists():
        raise HTTPException(status_code=404, detail="Video not found")

    b64 = extract_frame_b64(str(vid_path), position=position)
    if not b64:
        raise HTTPException(status_code=500, detail="Frame extraction failed -- check ffmpeg")

    frames_dir = OUTPUT_DIR / "frames"
    frames_dir.mkdir(exist_ok=True)

    ts = int(time.time() * 1000)
    pos_str = "first" if position < 0.1 else ("last" if position > 0.9 else "mid")
    out_name = f"frame_{pos_str}_{ts}.jpg"
    out_path = frames_dir / out_name
    out_path.write_bytes(base64.b64decode(b64))

    return {"path": str(out_path), "url": f"/output/frames/{out_name}"}


@app.post("/api/output/delete")
async def delete_output(request: Request):
    """Delete a single output file, or an entire job folder."""
    import shutil as _shutil
    body = await request.json()
    path = body.get("path", "")
    delete_folder = body.get("folder", False)

    file_path = Path(path).resolve() if os.path.isabs(path) else (OUTPUT_DIR / path).resolve()
    out_root = OUTPUT_DIR.resolve()
    if not str(file_path).startswith(str(out_root)):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    if not file_path.exists():
        return {"ok": True}  # already gone

    try:
        if delete_folder:
            # BUG-06: use explicit depth check instead of the old inverted logic.
            # depth == 2 -> output/date/jobfolder/file.mp4  (delete jobfolder)
            # depth == 1 -> output/jobfolder/file.mp4        (delete jobfolder)
            job_dir = file_path.parent
            try:
                depth = len(file_path.relative_to(out_root).parts)
            except ValueError:
                return JSONResponse({"error": "Forbidden"}, status_code=403)
            if depth >= 2 and str(job_dir).startswith(str(out_root)) and job_dir != out_root:
                _shutil.rmtree(str(job_dir), ignore_errors=True)
            else:
                return JSONResponse({"error": "Cannot delete -- unexpected depth"}, status_code=400)
        else:
            file_path.unlink(missing_ok=True)
        return {"ok": True}
    except PermissionError as e:
        # WinError 32: file locked by another process (browser streaming it).
        log.warning("delete_output locked: %s", e)
        return JSONResponse({"error": "File is in use (close the video player first, then delete)"}, status_code=423)
    except Exception as e:
        log.warning("delete_output error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/queue")
async def queue_status():
    if _g["job_manager"] is None:
        return JSONResponse({"error": "Not ready"}, 503)
    return _g["job_manager"].queue_status()


# -- Wildcards ----------------------------------------------------------------

@app.get("/api/wildcards")
async def get_wildcards():
    fs_root = cfg.get("sd_wildcards_dir") or ""
    return {"wildcards": wildcards.get_tokens(fs_root)}


@app.post("/api/wildcards/expand")
async def expand_wildcards(request: Request):
    body = await request.json()
    text = body.get("text", "")
    fs_root = cfg.get("sd_wildcards_dir") or ""
    return {"expanded": wildcards.expand(text, fs_root)}


# -- Session ------------------------------------------------------------------

@app.get("/api/session")
async def get_session_info():
    return session.get_current().to_dict()


@app.get("/api/session/files")
async def get_session_files():
    return {"files": session.get_current().get_all()}


@app.get("/api/session/videos")
async def get_session_videos():
    return {"videos": session.get_current().get_videos()}


@app.get("/api/session/images")
async def get_session_images():
    return {"images": session.get_current().get_images()}


@app.post("/api/session/new")
async def new_session_route():
    s = session.new_session()
    return s.to_dict()


@app.get("/api/sessions")
async def list_sessions():
    return {"sessions": session.list_sessions()}


@app.post("/api/session/switch/{session_id}")
async def switch_session(session_id: str):
    s = session.set_current(session_id)
    if s:
        return s.to_dict()
    return JSONResponse({"error": "Session not found"}, 404)


# -- Windows theme ------------------------------------------------------------

@app.get("/api/theme")
async def windows_theme():
    """Return Windows accent colour and dark/light mode from the registry."""
    try:
        import winreg
        dwm  = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM")
        abgr = winreg.QueryValueEx(dwm, "AccentColor")[0]          # stored as AABBGGRR
        r = abgr & 0xFF
        g = (abgr >> 8) & 0xFF
        b = (abgr >> 16) & 0xFF
        accent = f"#{r:02X}{g:02X}{b:02X}"

        pers = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                              r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        dark = winreg.QueryValueEx(pers, "AppsUseLightTheme")[0] == 0
    except Exception:
        accent = "#0078d4"   # Windows default blue fallback
        dark   = True
    return {"accent": accent, "dark": dark}


# -- System info --------------------------------------------------------------

@app.get("/api/system")
async def system_info():
    ollama_st = await asyncio.to_thread(keys.status)
    import json as _json
    import time as _time
    # Check if new commits have landed since this process started.
    # Cached for 60s to avoid spawning a git subprocess on every poll cycle
    # (frontend polls /api/system every 15s per tab).
    _boot_hash = _g.get("boot_git_hash")
    _cache = _g.get("_git_check_cache", {})
    if not _boot_hash:
        _restart_needed = False
    elif _time.time() - _cache.get("ts", 0) < 60:
        _restart_needed = _cache.get("result", False)
    else:
        _restart_needed = False
        try:
            import subprocess as _sp3
            _rn = _sp3.run(["git", "-C", str(APP_DIR), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=3)
            _restart_needed = (_rn.returncode == 0 and
                               _rn.stdout.strip() != _boot_hash)
        except Exception:
            pass
        _g["_git_check_cache"] = {"ts": _time.time(), "result": _restart_needed}

    data = {
        "ffmpeg": ffmpeg_available(),
        "encoders": [{"id": e[0], "label": e[1], "hw": e[2]} for e in _g["available_encoders"]],
        "best_encoder": best_encoder(_g["available_encoders"]),
        "gpu_vram_gb": _g.get("gpu_vram_gb"),
        "ollama": ollama_st,
        "services": svc.get_status(),
        "restart_needed": _restart_needed,
        "boot_git_hash": _boot_hash,
    }
    return JSONResponse(content=data, headers={"Cache-Control": "no-store"})


# -- AI intent (palette-driven) -----------------------------------------------

_AI_INTENT_TABS: dict[str, dict] = {
    "sd-prompts": {
        "schema": "steps (4-60), cfg (1-20), width/height (512-2048), sampler (str), scheduler (str), seed (int, -1 for random), prompt_append (str: extra tags to append), negative_append (str), smart_wildcards (bool), regional (bool), regions_n (1-4)",
        "system": (
            "You are a studio-control assistant. The user is on the SD Prompts tab. "
            "Convert their free-text request into a JSON mutation of the current settings. "
            "Output ONE json fenced block, nothing else. "
            "Keys allowed: steps, cfg, width, height, sampler, scheduler, seed, prompt_append, negative_append, smart_wildcards, regional, regions_n. "
            "Only include keys that need to change. "
            "prompt_append is comma-separated tags (no sentences) to add to the current prompt; "
            "negative_append same for the negative. "
            "If the user asks for more variety, turn smart_wildcards on. "
            "If the user asks for regional/Forge Couple, set regional:true and optionally regions_n. "
            "Output: ```json\\n{\"reply\": \"<one short sentence>\", \"settings\": {...}}\\n```"
        ),
    },
    "fun-videos": {
        "schema": "prompt_append (str, comma tags), steps (integer 4-50), guidance (1-20), duration_sec (seconds 2-20; 'slower' means LONGER duration, not shorter)",
        "system": (
            "You are a studio-control assistant. The user is on the Create Videos tab (WanGP I2V/T2V). "
            "Convert their free-text request into a JSON mutation of current settings. "
            "Output ONE json fenced block, nothing else. "
            "Keys allowed: prompt_append, steps, guidance, duration_sec. "
            "prompt_append is comma-separated motion/style tags to append. "
            "Output: ```json\\n{\"reply\": \"<one short sentence>\", \"settings\": {...}}\\n```"
        ),
    },
    "bridges": {
        "schema": "transition_mode (one of: cinematic, continuity, kinetic, surreal, meld, morph, shape_match, fade), creativity (integer 1-10, higher=wilder), bridge_length (seconds, 3-20), steps (integer 4-50)",
        "system": (
            "You are a studio-control assistant. The user is on the Video Bridges tab. "
            "Convert their free-text request into a JSON mutation of current settings. "
            "Output ONE json fenced block, nothing else. "
            "Keys allowed: transition_mode, creativity, bridge_length. "
            "transition_mode must be one of: cinematic, continuity, kinetic, surreal, meld, morph, shape_match, fade. "
            "Output: ```json\\n{\"reply\": \"<one short sentence>\", \"settings\": {...}}\\n```"
        ),
    },
}


_AI_INTENT_ALLOWED: dict[str, set[str]] = {
    "sd-prompts": {
        "steps", "cfg", "width", "height", "sampler", "scheduler", "seed",
        "prompt_append", "negative_append", "smart_wildcards", "regional", "regions_n",
    },
    "fun-videos": {"prompt_append", "steps", "guidance", "duration_sec"},
    "bridges":    {"transition_mode", "creativity", "bridge_length", "steps"},
}


def _allowed_intent_keys(tab: str) -> set[str]:
    return _AI_INTENT_ALLOWED.get(tab, set())


def _parse_intent_json(raw: str) -> dict:
    """Extract {reply, settings} from a fenced or bare JSON block."""
    import re as _re
    if not raw:
        return {}
    cleaned = _re.sub(r"<think>[\s\S]*?</think>\s*", "", raw).strip()
    m = _re.search(r"```json\s*([\s\S]*?)\s*```", cleaned, _re.IGNORECASE)
    candidate = m.group(1) if m else (cleaned if cleaned.startswith("{") else None)
    if not candidate:
        m2 = _re.search(r"\{[\s\S]*\}", cleaned)
        if not m2:
            return {}
        candidate = m2.group(0)
    try:
        return _json_std.loads(candidate)
    except Exception:
        try:
            return _json_std.loads(_re.sub(r",(\s*[}\]])", r"\1", candidate))
        except Exception:
            return {}


@app.post("/api/ai-intent")
async def ai_intent(request: Request):
    """Natural-language settings mutation for a given tab.

    Body: {tab, query, context}. Returns {reply, settings, provider_used}.
    Settings is a dict the tab-side applier uses to mutate controls.
    """
    body = await request.json()
    tab = (body.get("tab") or "").strip()
    query = (body.get("query") or "").strip()
    context = body.get("context") or {}
    if not query:
        raise HTTPException(400, "query required")
    tab_cfg = _AI_INTENT_TABS.get(tab)
    if not tab_cfg:
        raise HTTPException(400, f"unknown tab: {tab!r}")

    ctx_dump = _json_std.dumps(context, indent=2, default=str)[:2000]
    user_msg = (
        f"CURRENT SETTINGS:\n{ctx_dump}\n\n"
        f"VALID KEYS AND RANGES: {tab_cfg['schema']}\n\n"
        f"USER REQUEST: {query}"
    )

    llm = get_llm_router()
    try:
        raw = await asyncio.to_thread(
            llm.route,
            [{"role": "user", "content": user_msg}],
            tier=TIER_FAST,
            max_tokens=400,
            system=tab_cfg["system"],
        )
    except Exception as e:
        log.exception("ai-intent failed")
        raise HTTPException(500, f"ai-intent failed: {e}")

    parsed = _parse_intent_json(raw)
    reply = (parsed.get("reply") or "").strip() or "Adjusted."
    # LLMs often skip the {reply, settings} wrapper and just emit the settings
    # keys flat. Accept either shape: if there's a `settings` dict use it;
    # otherwise treat the top-level JSON as the settings dict (sans `reply`).
    settings_raw = parsed.get("settings")
    if isinstance(settings_raw, dict):
        settings = settings_raw
    else:
        settings = {k: v for k, v in parsed.items() if k != "reply"}
    # Drop junk keys we don't accept for this tab -- the JS applier ignores
    # unknown keys too, but filtering server-side keeps the toast count honest.
    allowed = _allowed_intent_keys(tab)
    settings = {k: v for k, v in settings.items() if k in allowed}
    if not settings:
        log.warning("ai-intent: parsed no settings from LLM. raw=%r parsed=%r", (raw or "")[:400], parsed)

    try:
        provider_used = llm._provider(None)  # noqa: SLF001
    except Exception:
        provider_used = "auto"

    return {"reply": reply, "settings": settings, "provider_used": provider_used}


# -- Feature routers ----------------------------------------------------------
from features.image2video.routes import router as i2v_router
from features.fun_videos.routes import router as fun_router
from features.video_bridges.routes import router as bridges_router
from features.sd_prompts.routes import router as prompts_router
from features.video_tools.routes import router as tools_router
from features.song_video.routes import router as song_router
from features.zoom.routes import router as zoom_router
from features.adobe_agent.routes import router as adobe_router
from features.retime.routes import router as retime_router
from features.lipsync.routes import router as lipsync_router

app.include_router(i2v_router, prefix="/api/i2v", tags=["Image to Video"])
app.include_router(fun_router, prefix="/api/fun", tags=["Create Videos"])
app.include_router(bridges_router, prefix="/api/bridges", tags=["Video Bridges"])
app.include_router(prompts_router, prefix="/api/prompts", tags=["SD Prompts"])
app.include_router(tools_router, prefix="/api/tools", tags=["Video Tools"])
app.include_router(song_router, prefix="/api/song-video", tags=["Song Video"])
app.include_router(zoom_router, tags=["Infinite Zoom"])
app.include_router(adobe_router, prefix="/api/adobe", tags=["Adobe Agent"])
app.include_router(retime_router, prefix="/api/retime", tags=["Retime"])
app.include_router(lipsync_router, prefix="/api/lipsync", tags=["Lip Sync"])


# -- Presets (WS8) ------------------------------------------------------------

_PRESETS_DB = APP_DIR / "presets.db"


def _presets_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_PRESETS_DB), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS presets (
            id TEXT PRIMARY KEY,
            tab TEXT,
            name TEXT,
            settings TEXT,
            created_at REAL
        )""")
    conn.commit()
    return conn


@app.get("/api/presets")
async def presets_list(tab: str = ""):
    def _query():
        conn = _presets_db()
        try:
            if tab:
                rows = conn.execute("SELECT * FROM presets WHERE tab = ? ORDER BY created_at DESC", (tab,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM presets ORDER BY created_at DESC").fetchall()
            return [_preset_to_dict(r) for r in rows]
        finally:
            conn.close()
    presets = await asyncio.to_thread(_query)
    return {"presets": presets}


def _preset_to_dict(row) -> dict:
    d = dict(row)
    try:
        d["settings"] = _json_std.loads(d.get("settings") or "{}")
    except Exception:
        d["settings"] = {}
    return d


@app.post("/api/presets")
async def preset_create(request: Request):
    body = await request.json()
    preset_id = str(uuid.uuid4())[:8]
    now = time.time()
    def _insert():
        conn = _presets_db()
        try:
            conn.execute(
                "INSERT INTO presets (id, tab, name, settings, created_at) VALUES (?, ?, ?, ?, ?)",
                (preset_id, body.get("tab", ""), body.get("name", "Preset"), _json_std.dumps(body.get("settings", {})), now)
            )
            conn.commit()
        finally:
            conn.close()
    await asyncio.to_thread(_insert)
    return {"id": preset_id, "tab": body.get("tab", ""), "name": body.get("name", "Preset"), "settings": body.get("settings", {}), "created_at": now}


@app.delete("/api/presets/{preset_id}")
async def preset_delete(preset_id: str):
    def _delete():
        conn = _presets_db()
        try:
            conn.execute("DELETE FROM presets WHERE id = ?", (preset_id,))
            conn.commit()
        finally:
            conn.close()
    await asyncio.to_thread(_delete)
    return {"ok": True}


# -- Gallery (WS2) ------------------------------------------------------------

_GALLERY_DB = APP_DIR / "gallery.db"

def _gallery_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_GALLERY_DB), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gallery (
            id TEXT PRIMARY KEY,
            tab TEXT,
            url TEXT,
            thumbnail TEXT,
            prompt TEXT,
            model TEXT,
            seed INTEGER,
            metadata TEXT,
            favorite INTEGER DEFAULT 0,
            created_at REAL
        )""")
    conn.commit()
    return conn


def gallery_push(url: str, tab: str = "", prompt: str = "", model: str = "", metadata: dict | None = None):
    """Insert a completed generation into gallery.db from server-side code.

    Called by pipelines so items appear in the gallery regardless of which
    browser tab is open when the job completes.  Silently skips duplicates
    (same url already in the db).
    """
    import uuid as _uuid
    meta = metadata or {}
    try:
        conn = _gallery_db()
        existing = conn.execute("SELECT id FROM gallery WHERE url=?", (url,)).fetchone()
        if existing:
            conn.close()
            return
        conn.execute("""
            INSERT INTO gallery (id, tab, url, thumbnail, prompt, model, seed, metadata, favorite, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            _uuid.uuid4().hex[:8], tab, url, "",
            prompt or meta.get("prompt", ""),
            model  or meta.get("model",  ""),
            None, _json_std.dumps(meta), 0, time.time(),
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.getLogger(__name__).warning("gallery_push failed: %s", e)


def _row_to_dict(row) -> dict:
    d = dict(row)
    try:
        d["metadata"] = _json_std.loads(d.get("metadata") or "{}")
    except Exception:
        d["metadata"] = {}
    d["favorite"] = bool(d.get("favorite"))
    return d


@app.get("/api/gallery")
async def gallery_list(tab: str = "", search: str = "", favorite: bool = False,
                       limit: int = 100, offset: int = 0):
    limit = max(1, min(limit, 500))
    def _query():
        conn = _gallery_db()
        try:
            clauses, params = [], []
            if tab:
                clauses.append("tab = ?"); params.append(tab)
            if search:
                clauses.append("(prompt LIKE ? OR model LIKE ?)"); params += [f"%{search}%", f"%{search}%"]
            if favorite:
                clauses.append("favorite = 1")
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            total = conn.execute(f"SELECT COUNT(*) FROM gallery {where}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM gallery {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            return [_row_to_dict(r) for r in rows], total
        finally:
            conn.close()
    items, total = await asyncio.to_thread(_query)
    return {"items": items, "total": total, "offset": offset, "limit": limit}


@app.post("/api/gallery")
async def gallery_create(request: Request):
    body = await request.json()
    item_id = str(uuid.uuid4())[:8]
    now = time.time()
    metadata = body.get("metadata", {})
    def _insert():
        conn = _gallery_db()
        try:
            conn.execute("""
                INSERT INTO gallery (id, tab, url, thumbnail, prompt, model, seed, metadata, favorite, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                item_id,
                body.get("tab", ""),
                body.get("url", ""),
                body.get("thumbnail", ""),
                body.get("prompt") or metadata.get("prompt", ""),
                body.get("model")  or metadata.get("model", ""),
                body.get("seed")   or metadata.get("seed"),
                _json_std.dumps(metadata),
                int(bool(body.get("favorite", False))),
                now,
            ))
            conn.commit()
        finally:
            conn.close()
    await asyncio.to_thread(_insert)
    return {
        "id": item_id, "tab": body.get("tab", ""), "url": body.get("url", ""),
        "thumbnail": body.get("thumbnail", ""), "prompt": body.get("prompt", ""),
        "metadata": metadata, "favorite": False, "created_at": now,
    }


@app.patch("/api/gallery/{item_id}")
async def gallery_update(item_id: str, request: Request):
    body = await request.json()
    def _update():
        conn = _gallery_db()
        try:
            fields = []
            params = []
            if "favorite" in body:
                fields.append("favorite = ?"); params.append(int(bool(body["favorite"])))
            if "prompt" in body:
                fields.append("prompt = ?"); params.append(body["prompt"])
            if not fields:
                return
            params.append(item_id)
            conn.execute(f"UPDATE gallery SET {', '.join(fields)} WHERE id = ?", params)
            conn.commit()
        finally:
            conn.close()
    await asyncio.to_thread(_update)
    return {"ok": True}


@app.delete("/api/gallery/{item_id}")
async def gallery_delete(item_id: str):
    def _delete():
        conn = _gallery_db()
        try:
            conn.execute("DELETE FROM gallery WHERE id = ?", (item_id,))
            conn.commit()
        finally:
            conn.close()
    await asyncio.to_thread(_delete)
    return {"ok": True}


# -- Entry point --------------------------------------------------------------

class _NoiseFilter(logging.Filter):
    """Drop high-frequency polling and harmless TCP noise from all log handlers.

    Two categories of noise:
    1. HTTP access-log entries from endpoints that poll every 1-3s -- no
       diagnostic value and drown out real events.
    2. ConnectionResetError / WinError 10054 -- asyncio TCP teardown when
       Chrome closes a video-stream connection.  Not an error; fires a
       10-line traceback for every 206 Partial Content response.
    """

    _SKIP_PATHS = (
        "/api/queue",
        "/api/jobs",
        "/api/logs",
        "/api/services",
        "/api/gpu/status",
        "/api/gallery?limit=",
        "/api/fun/models",
        "/api/llm/config",
        "/api/prompts/forge/status",
        "/api/version",
        "/api/system",
        "/manifest.json",
        "/sw.js",
    )

    _SKIP_MSGS = (
        "ConnectionResetError",
        "WinError 10054",
        "_call_connection_lost",
        "socket.SHUT_RDWR",
        "NoneType: None",   # bare exception-with-no-value that asyncio emits alongside the above
    )

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if any(s in msg for s in self._SKIP_PATHS):
            return False
        if any(s in msg for s in self._SKIP_MSGS):
            return False
        # Also suppress exception info that is a ConnectionResetError
        if record.exc_info:
            exc_type = record.exc_info[0]
            if exc_type is not None and issubclass(exc_type, ConnectionResetError):
                return False
        return True


_noise_filter = _NoiseFilter()

# Attach to every handler so records propagated from child loggers are
# also filtered (Python only checks logger-level filters for that logger's
# own handlers, not for parent handlers that receive propagated records).
for _h in logging.root.handlers:
    _h.addFilter(_noise_filter)

# Also filter at the source loggers so the records never propagate at all
logging.getLogger("uvicorn.access").addFilter(_noise_filter)
logging.getLogger("asyncio").addFilter(_noise_filter)


if __name__ == "__main__":
    import uvicorn
    # Pick the first free port from 7860..7879 so we don't collide with Forge,
    # WanGP, another DCS instance, or any unrelated app that grabbed 7860.
    try:
        _port = port_lock.find_free_port()
    except RuntimeError as e:
        log.error("port discovery failed: %s", e)
        raise SystemExit(1)
    _g["port"] = _port
    port_lock.write_port_file(_port)
    try:
        uvicorn.run(app, host="127.0.0.1", port=_port)
    finally:
        port_lock.clear_port_file()
