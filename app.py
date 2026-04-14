"""Drop Cat Go Studio — Unified Video Production App.

Single FastAPI server combining Fun Videos, Video Bridges, SD Prompts,
Image-to-Video, Video Tools, and WanGP/ACE-Step service management.
"""
# Fix double-import: when launched as `python app.py`, this file is loaded as
# __main__. Feature routes later do `from app import get_llm_router`, which
# triggers a *second* import of app.py as a fresh `app` module with its own
# empty `_g` dict -- causing the "not initialized" error. Registering ourselves
# as "app" here ensures both names share the same module object.
import sys as _sys
_sys.modules.setdefault("app", _sys.modules.get("__main__"))

import logging
import logging.handlers
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core import config as cfg
from core import keys, log_buffer, session, wildcards
from core.ffmpeg_utils import ffmpeg_available
from core.hw_encoders import detect_encoders, best_encoder
from core.job_manager import JobManager
from core.llm_client import LLMClient
from core.llm_router import LLMRouter
from services import manager as svc

# ── Logging setup ────────────────────────────────────────────────────────────

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

# ── Globals (initialized in lifespan) ────────────────────────────────────────
# Use a mutable dict — dict item mutation is always visible module-wide without
# relying on 'global' inside async generators (broken in Python 3.10 asynccontextmanager).

_g: dict = {
    "job_manager":        None,
    "llm_client":         None,
    "llm_router":         None,
    "available_encoders": [],
}

APP_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = APP_DIR / "uploads"
OUTPUT_DIR = APP_DIR / "output"
STATIC_DIR = APP_DIR / "static"

# BUG-01: create directories at module level so StaticFiles mounts succeed on
# fresh install (StaticFiles checks directory existence in __init__).
UPLOADS_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)


# ── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Drop Cat Go Studio starting up...")

    # Ensure runtime directories exist
    UPLOADS_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    # Migrate config from old apps on first run
    cfg.migrate_from_old_apps()

    # Initialize job manager
    _g["job_manager"] = JobManager()

    # Initialize LLM client + router
    _g["llm_client"] = LLMClient(
        host=cfg.get("ollama_host") or "http://localhost:11434",
        fast_model=cfg.get("ollama_fast_model") or "qwen3-vl:8b",
        balanced_model=cfg.get("ollama_balanced_model") or "qwen3-vl:8b",
        power_model=cfg.get("ollama_power_model") or "qwen3-vl:30b",
    )
    _g["llm_router"] = LLMRouter(_g["llm_client"])
    # Auto-seed Anthropic key from credentials file if not already saved
    if not cfg.get("anthropic_key") and keys.get_key("anthropic"):
        cfg.save({"anthropic_key": keys.get_key("anthropic")})
        log.info("Auto-loaded Anthropic key from credentials file")

    # Detect GPU encoders
    _g["available_encoders"] = detect_encoders()
    if _g["available_encoders"]:
        hw = [e[1] for e in _g["available_encoders"] if e[2]]
        log.info("Encoders: %s", ", ".join(hw) if hw else "CPU only")

    # Start external services (WanGP, ACE-Step) in background
    threading.Thread(target=svc.startup_all, daemon=True).start()

    # Periodic job cleanup — purge completed/errored jobs older than 24h
    def _cleanup_jobs():
        import time as _time
        while True:
            _time.sleep(3600)
            jm = _g.get("job_manager")
            if jm:
                jm.cleanup()
    threading.Thread(target=_cleanup_jobs, daemon=True).start()

    # System tray icon (Windows only, optional — skips silently if pystray missing)
    from core import tray as _tray
    _tray.start_tray()

    log.info("Drop Cat Go Studio ready on http://127.0.0.1:7860")

    yield

    # Shutdown
    log.info("Shutting down...")
    svc.shutdown_all()


# ── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(title="Drop Cat Go Studio", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files and media directories (before routes for correct priority)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")
# NOTE: /output is served via a route below, not StaticFiles, because Starlette's
# StaticFiles path joining on Windows breaks for nested subdirectories.


# ── Global accessors (use these in features instead of direct import) ─────────

def get_llm_router():
    """Return the initialized LLMRouter."""
    r = _g["llm_router"]
    if r is None:
        raise RuntimeError("LLM router not initialized — app not fully started")
    return r


def get_job_manager():
    """Return the initialized JobManager."""
    jm = _g["job_manager"]
    if jm is None:
        raise RuntimeError("Job manager not initialized — app not fully started")
    return jm


# ── Global routes ────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return JSONResponse({"status": "Drop Cat Go Studio is running", "ui": "not built yet"})


# ── Config ───────────────────────────────────────────────────────────────────

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


# ── Ollama config ─────────────────────────────────────────────────────────────

@app.get("/api/ollama/models")
async def ollama_models():
    """Return list of Ollama model names installed locally."""
    return {"models": keys.get_ollama_models()}


@app.post("/api/ollama/config")
async def save_ollama_config(request: Request):
    """Save Ollama host and model preferences, hot-reload client."""
    body = await request.json()
    updates = {}
    for key in ("ollama_host", "ollama_fast_model", "ollama_balanced_model", "ollama_power_model"):
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
            )
    return keys.status()


# Legacy key status endpoint (returns Ollama status now)
@app.get("/api/keys/status")
async def keys_status():
    return keys.status()


# ── LLM provider config ───────────────────────────────────────────────────────

@app.get("/api/llm/config")
async def get_llm_config():
    """Return current LLM provider and whether keys are set."""
    provider = cfg.get("llm_provider") or "ollama"
    anthropic_key = keys.get_key("anthropic")
    openai_key = keys.get_key("openai")
    return {
        "provider": provider,
        "anthropic_key_set": bool(anthropic_key),
        "openai_key_set": bool(openai_key),
        # Return masked keys for display (show last 4 chars)
        "anthropic_key_hint": f"...{anthropic_key[-4:]}" if len(anthropic_key) > 4 else ("set" if anthropic_key else ""),
        "openai_key_hint": f"...{openai_key[-4:]}" if len(openai_key) > 4 else ("set" if openai_key else ""),
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


# ── Services ─────────────────────────────────────────────────────────────────

@app.get("/api/services")
async def services_status():
    return svc.get_status()


@app.post("/api/services/start/{name}")
async def start_service(name: str):
    starters = {
        "wangp": svc.start_wangp_worker,
        "acestep": svc.start_acestep,
        "forge": svc.start_forge,
        "ollama": svc.start_ollama,
    }
    fn = starters.get(name)
    if not fn:
        return JSONResponse({"error": f"Unknown service: {name}"}, 404)
    # Run in background thread -- services can take minutes to start
    threading.Thread(target=fn, daemon=True).start()
    return {"ok": True, "message": f"Starting {name}..."}


@app.post("/api/services/stop/{name}")
async def stop_service_route(name: str):
    ok, err = svc.stop_service(name)
    return {"ok": ok, "error": err}


@app.post("/api/services/restart/{name}")
async def restart_service_route(name: str):
    threading.Thread(target=svc.restart_service, args=(name,), daemon=True).start()
    return {"ok": True, "message": f"Restarting {name}..."}


# ── Logs ─────────────────────────────────────────────────────────────────────

@app.get("/api/logs")
async def get_logs(since: int = 0):
    if since:
        return {"logs": log_buffer.get_since(since)}
    return {"logs": log_buffer.get_recent()}


@app.get("/api/logs/file")
async def get_log_file(lines: int = 200):
    """Return the last N lines from the persistent log file as plain text."""
    from fastapi.responses import PlainTextResponse
    if not _LOG_FILE.exists():
        return PlainTextResponse("Log file not found yet — restart the app.\n")
    try:
        all_lines = _LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(all_lines[-lines:])
        return PlainTextResponse(tail + "\n")
    except Exception as e:
        return PlainTextResponse(f"Error reading log file: {e}\n")


# ── Jobs ─────────────────────────────────────────────────────────────────────

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
        raise HTTPException(status_code=500, detail="Frame extraction failed — check ffmpeg")

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
            # depth == 2 → output/date/jobfolder/file.mp4  (delete jobfolder)
            # depth == 1 → output/jobfolder/file.mp4        (delete jobfolder)
            job_dir = file_path.parent
            try:
                depth = len(file_path.relative_to(out_root).parts)
            except ValueError:
                return JSONResponse({"error": "Forbidden"}, status_code=403)
            if depth >= 2 and str(job_dir).startswith(str(out_root)) and job_dir != out_root:
                _shutil.rmtree(str(job_dir), ignore_errors=True)
            else:
                return JSONResponse({"error": "Cannot delete — unexpected depth"}, status_code=400)
        else:
            file_path.unlink(missing_ok=True)
        return {"ok": True}
    except Exception as e:
        log.warning("delete_output error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/queue")
async def queue_status():
    if _g["job_manager"] is None:
        return JSONResponse({"error": "Not ready"}, 503)
    return _g["job_manager"].queue_status()


# ── Wildcards ────────────────────────────────────────────────────────────────

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


# ── Session ──────────────────────────────────────────────────────────────────

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


# ── System info ──────────────────────────────────────────────────────────────

@app.get("/api/system")
async def system_info():
    ollama_st = keys.status()  # {"ollama": bool, "models": [...]}
    return {
        "ffmpeg": ffmpeg_available(),
        "encoders": [{"id": e[0], "label": e[1], "hw": e[2]} for e in _g["available_encoders"]],
        "best_encoder": best_encoder(_g["available_encoders"]),
        "ollama": ollama_st,
        "services": svc.get_status(),
    }


# ── Feature routers ──────────────────────────────────────────────────────────
from features.image2video.routes import router as i2v_router
from features.fun_videos.routes import router as fun_router
from features.video_bridges.routes import router as bridges_router
from features.sd_prompts.routes import router as prompts_router
from features.video_tools.routes import router as tools_router

app.include_router(i2v_router, prefix="/api/i2v", tags=["Image to Video"])
app.include_router(fun_router, prefix="/api/fun", tags=["Fun Videos"])
app.include_router(bridges_router, prefix="/api/bridges", tags=["Video Bridges"])
app.include_router(prompts_router, prefix="/api/prompts", tags=["SD Prompts"])
app.include_router(tools_router, prefix="/api/tools", tags=["Video Tools"])


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=7860)
