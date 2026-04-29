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

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core import config as cfg
from core import keys, log_buffer, port_lock, session, wildcards
from core.ffmpeg_utils import ffmpeg_available
from core.hw_encoders import detect_encoders, best_encoder
from core.job_manager import JobManager
from core.llm_client import LLMClient
from core.llm_router import LLMRouter, TIER_BALANCED, TIER_FAST
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
        return f"{date} · {sha}" if date and sha else "unknown"
    except Exception:
        return "unknown"


APP_VERSION = _read_git_version()


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
        vision_model=cfg.get("ollama_vision_model") or "qwen3-vl:8b",
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

    # Background: detect current state then start any stopped workers
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

    # System tray icon — skip when launched from manager.pyw (it has its own)
    if not os.environ.get("DCS_MANAGED"):
        from core import tray as _tray
        _tray.start_tray()

    _port = _g.get("port", 7860)
    log.info("Drop Cat Go Studio ready on http://127.0.0.1:%d", _port)

    yield

    # Shutdown
    log.info("Shutting down...")
    svc.shutdown_all()
    port_lock.clear_port_file()


# ── App ──────────────────────────────────────────────────────────────────────

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

_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate", "Pragma": "no-cache"}

@app.get("/")
async def index():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path), headers=_NO_CACHE)
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


# ── LLM provider config ───────────────────────────────────────────────────────

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


# ── Services ─────────────────────────────────────────────────────────────────

@app.get("/api/services")
async def services_status():
    return JSONResponse(content=svc.get_status(), headers={"Cache-Control": "no-store"})


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


@app.post("/api/logs/client")
async def client_log(request: Request):
    """Receive browser-side errors and write them to the server log."""
    try:
        body = await request.json()
        msg  = body.get("message", "unknown client error")
        src  = body.get("source", "")
        line = body.get("lineno", "")
        logger.error("CLIENT JS: %s  (%s:%s)", msg, src, line)
    except Exception:
        pass
    return {"ok": True}


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


@app.get("/api/jobs")
async def list_jobs():
    if _g["job_manager"] is None:
        return JSONResponse({"error": "Not ready"}, 503)
    return _g["job_manager"].queue_status()


@app.get("/api/thumbnail")
async def get_thumbnail(path: str, size: int = 120):
    """Serve a scaled-down thumbnail of any image path."""
    import io
    from pathlib import Path as _Path
    from fastapi.responses import Response as _Resp
    try:
        from PIL import Image as _Img
        p = _Path(path)
        if not p.is_file():
            return JSONResponse({"error": "Not found"}, 404)
        with _Img.open(p) as img:
            img.thumbnail((size * 2, size * 2))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=75)
        return _Resp(content=buf.getvalue(), media_type="image/jpeg")
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)


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


# ── Windows theme ────────────────────────────────────────────────────────────

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


# ── System info ──────────────────────────────────────────────────────────────

@app.get("/api/system")
async def system_info():
    ollama_st = await asyncio.to_thread(keys.status)
    import json as _json
    data = {
        "ffmpeg": ffmpeg_available(),
        "encoders": [{"id": e[0], "label": e[1], "hw": e[2]} for e in _g["available_encoders"]],
        "best_encoder": best_encoder(_g["available_encoders"]),
        "ollama": ollama_st,
        "services": svc.get_status(),
    }
    return JSONResponse(content=data, headers={"Cache-Control": "no-store"})


# ── AI intent (palette-driven) ───────────────────────────────────────────────

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
    # Drop junk keys we don't accept for this tab — the JS applier ignores
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


# ── Presets (WS8) ────────────────────────────────────────────────────────────

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


# ── Gallery (WS2) ────────────────────────────────────────────────────────────

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


def _row_to_dict(row) -> dict:
    d = dict(row)
    try:
        d["metadata"] = _json_std.loads(d.get("metadata") or "{}")
    except Exception:
        d["metadata"] = {}
    d["favorite"] = bool(d.get("favorite"))
    return d


@app.get("/api/gallery")
async def gallery_list(tab: str = "", search: str = "", favorite: bool = False):
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
            rows = conn.execute(f"SELECT * FROM gallery {where} ORDER BY created_at DESC LIMIT 500", params).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            conn.close()
    items = await asyncio.to_thread(_query)
    return {"items": items}


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


# ── Entry point ──────────────────────────────────────────────────────────────

class _NoiseFilter(logging.Filter):
    """Drop high-frequency polling and harmless TCP noise from all log handlers.

    Two categories of noise:
    1. HTTP access-log entries from endpoints that poll every 1-3s — no
       diagnostic value and drown out real events.
    2. ConnectionResetError / WinError 10054 — asyncio TCP teardown when
       Chrome closes a video-stream connection.  Not an error; fires a
       10-line traceback for every 206 Partial Content response.
    """

    _SKIP_PATHS = (
        "/api/queue",
        "/api/jobs",
        "/api/logs",
        "/api/services",
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
