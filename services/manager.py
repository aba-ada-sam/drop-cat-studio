"""Service detection and auto-launch for WanGP and ACE-Step.

Adapted from DropCatGo-Fun-Videos_w_Audio/services.py. Manages external
AI services that multiple features depend on.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from core import config as cfg

log = logging.getLogger(__name__)

# ── Status tracking ──────────────────────────────────────────────────────────

_status_lock = threading.Lock()
_service_status: dict = {
    "wangp":   {"state": "unknown", "message": "", "port": None, "pid": None},
    "acestep": {"state": "unknown", "message": "", "port": None, "pid": None},
    "forge":   {"state": "unknown", "message": "", "port": 7861, "pid": None},
    "void":    {"state": "unknown", "message": "", "port": 7901, "pid": None},
}

_wangp_worker_proc: subprocess.Popen | None = None
_acestep_proc: subprocess.Popen | None = None
_void_proc: subprocess.Popen | None = None

# BUG-11: guard against two concurrent calls both passing the alive-check and
# starting duplicate worker processes.
_wangp_start_lock = threading.Lock()
_void_start_lock  = threading.Lock()


def get_status() -> dict:
    with _status_lock:
        return {k: dict(v) for k, v in _service_status.items()}


def _set_status(service: str, **kwargs):
    with _status_lock:
        _service_status[service].update(kwargs)


# ── Port / HTTP helpers ──────────────────────────────────────────────────────

def check_port(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def http_get(url: str, timeout: int = 5) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


# ── ACE-Step ─────────────────────────────────────────────────────────────────

ACESTEP_HOST = "127.0.0.1"
ACESTEP_PORT = 8019


def acestep_alive() -> bool:
    return http_get(f"http://{ACESTEP_HOST}:{ACESTEP_PORT}/health", timeout=3) is not None


def start_acestep() -> tuple[bool, str | None]:
    """Start ACE-Step API server headlessly if not already running.

    Supports both venv-based (.venv/Scripts/python.exe) and uv-based installs.
    """
    import shutil
    global _acestep_proc

    if acestep_alive():
        _set_status("acestep", state="running",
                    message="ACE-Step server already running", port=ACESTEP_PORT)
        log.info("ACE-Step already running on port %d", ACESTEP_PORT)
        return True, None

    acestep_root = cfg.get_acestep_root()
    if acestep_root is None:
        msg = "ACE-Step path not configured — set it in Settings"
        _set_status("acestep", state="not_configured", message=msg)
        return False, msg

    api_script = acestep_root / "acestep" / "api_server.py"
    if not api_script.exists():
        msg = f"ACE-Step api_server.py not found: {api_script}"
        _set_status("acestep", state="error", message=msg)
        return False, msg

    # Prefer venv python; fall back to uv run
    python = acestep_root / ".venv" / "Scripts" / "python.exe"
    uv_exe = shutil.which("uv")

    if python.exists():
        cmd = [str(python), str(api_script), "--host", ACESTEP_HOST, "--port", str(ACESTEP_PORT)]
    elif uv_exe:
        cmd = [uv_exe, "run", "--no-sync", "acestep-api",
               "--host", ACESTEP_HOST, "--port", str(ACESTEP_PORT)]
    else:
        msg = "Cannot start ACE-Step: no .venv Python and 'uv' not found in PATH"
        _set_status("acestep", state="error", message=msg)
        return False, msg

    _set_status("acestep", state="starting", message="Starting ACE-Step server...")
    log.info("Starting ACE-Step server headlessly (port %d)...", ACESTEP_PORT)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["ACESTEP_NO_INIT"] = "false"
    env["ACESTEP_INIT_LLM"] = "auto"
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(acestep_root) + (os.pathsep + existing_pp if existing_pp else "")

    try:
        proc = subprocess.Popen(
            cmd, cwd=str(acestep_root), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        _acestep_proc = proc

        def _drain(p):
            try:
                for line in p.stdout:
                    stripped = line.rstrip()
                    if stripped:
                        log.info("[ace-step] %s", stripped)
            except Exception:
                pass

        threading.Thread(target=_drain, args=(proc,), daemon=True).start()

        deadline = time.time() + 300  # 5 min — LM model loading takes ~90s

        # Phase 1: wait for uvicorn to bind the port (~5s)
        while time.time() < deadline:
            if check_port(ACESTEP_HOST, ACESTEP_PORT, timeout=2):
                break
            if proc.poll() is not None:
                msg = "ACE-Step process exited before port opened"
                _set_status("acestep", state="error", message=msg)
                return False, msg
            time.sleep(2)
        else:
            msg = "ACE-Step did not open port within 300s"
            _set_status("acestep", state="error", message=msg)
            return False, msg

        # Phase 2: port open; wait for model loading (~90s).
        # uvicorn queues HTTP requests during startup — issue one long-timeout
        # request so we get the response as soon as initialization finishes.
        log.info("ACE-Step port %d open — waiting for model to load...", ACESTEP_PORT)
        _set_status("acestep", state="starting",
                    message="ACE-Step loading model into VRAM (~90s)...")
        while time.time() < deadline:
            remaining = max(10, int(deadline - time.time()))
            result = http_get(
                f"http://{ACESTEP_HOST}:{ACESTEP_PORT}/health",
                timeout=remaining,
            )
            if result is not None:
                _set_status("acestep", state="running",
                            message="ACE-Step ready — music generation available",
                            port=ACESTEP_PORT, pid=proc.pid)
                log.info("ACE-Step started and ready on port %d", ACESTEP_PORT)
                return True, None
            if proc.poll() is not None:
                msg = "ACE-Step exited during model loading"
                _set_status("acestep", state="error", message=msg)
                return False, msg
            time.sleep(5)

        msg = "ACE-Step model did not finish loading within 300s"
        _set_status("acestep", state="error", message=msg)
        return False, msg

    except Exception as e:
        msg = f"Failed to start ACE-Step: {e}"
        _set_status("acestep", state="error", message=msg)
        return False, msg


# ── WanGP ────────────────────────────────────────────────────────────────────

WANGP_GRADIO_PORTS = [7860, 7861, 7862, 7863]
WANGP_WORKER_PORT = 7899


def _detect_wangp_gradio() -> int | None:
    for port in WANGP_GRADIO_PORTS:
        if check_port("127.0.0.1", port):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/info", timeout=3) as r:
                    data = r.read().decode().lower()
                    if "gradio" in data or "version" in data:
                        return port
            except Exception:
                pass
    return None


def wangp_worker_alive() -> bool:
    return check_port("127.0.0.1", WANGP_WORKER_PORT, timeout=2)


def check_wangp() -> dict:
    wan_root = cfg.get("wan2gp_root")
    if not wan_root:
        return {"state": "not_configured", "message": "WanGP path not configured"}
    ok, msg = cfg.validate_wan2gp(wan_root)
    if not ok:
        return {"state": "error", "message": msg}
    if wangp_worker_alive():
        return {"state": "running", "message": "WanGP worker running (model loaded)", "mode": "worker"}
    gradio_port = _detect_wangp_gradio()
    if gradio_port:
        return {"state": "running", "message": f"WanGP Gradio on port {gradio_port}", "mode": "gradio", "port": gradio_port}
    return {"state": "ready", "message": "WanGP configured — will use subprocess per request", "mode": "subprocess"}


def start_wangp_worker() -> tuple[bool, str | None]:
    """Start the persistent WanGP worker process.

    BUG-11: _wangp_start_lock serializes concurrent calls so only one
    subprocess is ever launched (two rapid UI clicks could otherwise both
    pass the alive check and each start a worker, leaking the first).
    """
    global _wangp_worker_proc

    with _wangp_start_lock:
        if wangp_worker_alive():
            _set_status("wangp", state="running", message="WanGP worker already running")
            return True, None

        wan_root = cfg.get("wan2gp_root")
        if not wan_root:
            msg = "WanGP path not configured — set it in Settings"
            _set_status("wangp", state="not_configured", message=msg)
            return False, msg

        ok, val_msg = cfg.validate_wan2gp(wan_root)
        if not ok:
            _set_status("wangp", state="error", message=val_msg)
            return False, val_msg

        python_exe = cfg.get("wan2gp_python") or cfg.find_wan_python(wan_root)
        worker_script = str(Path(__file__).parent / "wangp_worker.py")

        if not Path(worker_script).exists():
            msg = "wangp_worker.py not found in services/"
            _set_status("wangp", state="error", message=msg)
            return False, msg

        _set_status("wangp", state="starting",
                    message="Starting WanGP worker (loading model)...")
        log.info("Starting WanGP worker -- loading model into VRAM...")

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        try:
            proc = subprocess.Popen(
                [python_exe, worker_script,
                 "--wangp-app", wan_root,
                 "--port", str(WANGP_WORKER_PORT)],
                cwd=wan_root, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
            _wangp_worker_proc = proc

            def _drain(p):
                try:
                    for line in p.stdout:
                        stripped = line.rstrip()
                        if stripped:
                            log.info("[wangp-worker] %s", stripped)
                except Exception:
                    pass

            threading.Thread(target=_drain, args=(proc,), daemon=True).start()

            deadline = time.time() + 180
            while time.time() < deadline:
                if wangp_worker_alive():
                    _set_status("wangp", state="running",
                                message="WanGP worker running (model loaded in VRAM)",
                                port=WANGP_WORKER_PORT, pid=proc.pid)
                    log.info("WanGP worker started -- model loaded")
                    return True, None
                if proc.poll() is not None:
                    msg = "WanGP worker exited during startup -- check GPU memory"
                    _set_status("wangp", state="error", message=msg)
                    return False, msg
                time.sleep(3)

            msg = "WanGP worker did not start within 180s"
            _set_status("wangp", state="error", message=msg)
            return False, msg

        except FileNotFoundError:
            msg = f"WanGP Python not found: {python_exe}"
            _set_status("wangp", state="error", message=msg)
            return False, msg
        except Exception as e:
            msg = f"Failed to start WanGP worker: {e}"
            _set_status("wangp", state="error", message=msg)
            return False, msg


# ── VOID ──────────────────────────────────────────────────────────────────────

VOID_PORT = 7901


def void_worker_alive() -> bool:
    return check_port("127.0.0.1", VOID_PORT, timeout=2)


def start_void_worker() -> tuple[bool, str | None]:
    """Start the Netflix VOID inpainting worker process."""
    global _void_proc

    with _void_start_lock:
        if void_worker_alive():
            _set_status("void", state="running", message="VOID worker already running")
            return True, None

        worker_script = str(Path(__file__).parent / "void_worker.py")
        if not Path(worker_script).exists():
            msg = "void_worker.py not found in services/"
            _set_status("void", state="error", message=msg)
            return False, msg

        model_dir = cfg.get("void_model_dir") or ""
        python_exe = "python"

        _set_status("void", state="starting", message="Starting VOID worker (loading model)...")
        log.info("Starting VOID worker on port %d...", VOID_PORT)

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        cmd = [python_exe, worker_script, "--port", str(VOID_PORT)]
        if model_dir:
            cmd += ["--model-dir", model_dir]

        try:
            proc = subprocess.Popen(
                cmd, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
            _void_proc = proc

            def _drain(p):
                try:
                    for line in p.stdout:
                        stripped = line.rstrip()
                        if stripped:
                            log.info("[void-worker] %s", stripped)
                except Exception:
                    pass

            threading.Thread(target=_drain, args=(proc,), daemon=True).start()

            deadline = time.time() + 30  # server starts fast; model loads in bg
            while time.time() < deadline:
                if void_worker_alive():
                    _set_status("void", state="starting",
                                message="VOID worker started (model loading in background...)",
                                port=VOID_PORT, pid=proc.pid)
                    log.info("VOID worker HTTP server up on port %d", VOID_PORT)
                    return True, None
                if proc.poll() is not None:
                    msg = "VOID worker process exited unexpectedly"
                    _set_status("void", state="error", message=msg)
                    return False, msg
                time.sleep(1)

            msg = "VOID worker did not start within 30s"
            _set_status("void", state="error", message=msg)
            return False, msg

        except Exception as e:
            msg = f"Failed to start VOID worker: {e}"
            _set_status("void", state="error", message=msg)
            return False, msg


# ── Lifecycle ────────────────────────────────────────────────────────────────

def startup_all():
    """Detect and start services concurrently on app startup."""
    log.info("Checking services...")
    threads = []

    def _start_ace():
        if acestep_alive():
            _set_status("acestep", state="running",
                        message="ACE-Step already running", port=ACESTEP_PORT)
            return
        # Auto-detect if not configured
        root = cfg.get_acestep_root()
        if root is None:
            detected = cfg.auto_detect_acestep()
            if detected:
                log.info("ACE-Step auto-detected at %s — saving to config", detected)
                cfg.save({"acestep_root": detected})
        start_acestep()

    def _start_wan():
        wangp_status = check_wangp()
        _set_status("wangp", **wangp_status)
        wan_root = cfg.get("wan2gp_root")
        if wan_root and wangp_status["state"] == "ready":
            start_wangp_worker()
        elif wangp_status["state"] == "not_configured":
            log.info("WanGP path not configured")

    def _check_forge():
        from services.forge_client import forge_alive, FORGE_PORT
        # Quick check first
        if forge_alive():
            _set_status("forge", state="running",
                        message="Forge WebUI running (SD image generation ready)",
                        port=FORGE_PORT)
            log.info("Forge WebUI detected on port %d", FORGE_PORT)
            return
        # Not up yet — mark as starting and retry for 90s (Forge takes ~60s to boot)
        _set_status("forge", state="starting",
                    message="Forge starting up, please wait (~60s)...")
        deadline = time.time() + 90
        while time.time() < deadline:
            time.sleep(5)
            if forge_alive():
                _set_status("forge", state="running",
                            message="Forge WebUI running (SD image generation ready)",
                            port=FORGE_PORT)
                log.info("Forge WebUI ready on port %d", FORGE_PORT)
                return
        # Gave up after 90s
        _set_status("forge", state="not_running",
                    message="Forge not detected — start it with --api flag")

    # FLW-10: fire-and-forget -- each _start_* function logs its own completion.
    # Joining would block the startup thread for up to 5 min (ACE-Step + WanGP).
    for fn in (_start_ace, _start_wan, _check_forge):
        threading.Thread(target=fn, daemon=True).start()

    log.info("Service startup initiated (background)")


def shutdown_all():
    """Cleanly shut down managed services."""
    global _wangp_worker_proc, _acestep_proc, _void_proc
    if _wangp_worker_proc and _wangp_worker_proc.poll() is None:
        _wangp_worker_proc.terminate()
        _wangp_worker_proc = None
    if _acestep_proc and _acestep_proc.poll() is None:
        _acestep_proc.terminate()
        _acestep_proc = None
    if _void_proc and _void_proc.poll() is None:
        _void_proc.terminate()
        _void_proc = None
