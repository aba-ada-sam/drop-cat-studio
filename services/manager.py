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

import sys

from core import config as cfg

log = logging.getLogger(__name__)


def _popen_flags() -> dict:
    """Return creationflags to hide console windows on Windows (unless debug mode)."""
    if sys.platform == "win32" and not cfg.get("debug_mode"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}

# ── Status tracking ──────────────────────────────────────────────────────────

_status_lock = threading.Lock()
_service_status: dict = {
    "wangp":   {"state": "unknown", "message": "", "port": None, "pid": None},
    "acestep": {"state": "unknown", "message": "", "port": None, "pid": None},
    "forge":   {"state": "unknown", "message": "", "port": 7861, "pid": None},
    "ollama":  {"state": "unknown", "message": "", "port": 11434, "pid": None},
}

_wangp_worker_proc: subprocess.Popen | None = None
_acestep_proc: subprocess.Popen | None = None

# BUG-11: guard against two concurrent calls both passing the alive-check and
# starting duplicate worker processes.
_wangp_start_lock = threading.Lock()


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
            **_popen_flags(),
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
                **_popen_flags(),
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

    # FLW-10: fire-and-forget -- each _start_* function logs its own completion.
    # Joining would block the startup thread for up to 5 min (ACE-Step + WanGP).
    def _safe_run(name, fn):
        try:
            fn()
        except Exception as e:
            log.error("[Startup] %s failed to start: %s", name, e)
            _set_status(name.lower(), state="error", message=f"Startup failed: {e}")

    def _check_forge():
        from services.forge_client import forge_alive, FORGE_PORT
        if forge_alive():
            _set_status("forge", state="running",
                        message="Forge WebUI running (SD image generation ready)",
                        port=FORGE_PORT)
            log.info("Forge WebUI detected on port %d", FORGE_PORT)
            return
        # Try to auto-start Forge
        ok, err = start_forge()
        if not ok and err:
            _set_status("forge", state="not_running",
                        message="Forge not running -- use Services tab to start")

    def _ensure_ollama():
        ok, err = start_ollama()
        if ok:
            _set_status("ollama", state="running",
                        message="Ollama running on port 11434", port=11434)
        else:
            _set_status("ollama", state="not_running",
                        message=err or "Ollama not available")

    for label, fn in [("WanGP", _start_wan), ("Forge", _check_forge), ("Ollama", _ensure_ollama)]:
        threading.Thread(target=_safe_run, args=(label, fn), daemon=True).start()

    # ACE-Step: deferred -- only started when music generation is needed.
    # Keeps VRAM free for Ollama vision models during prompt generation.
    if acestep_alive():
        _set_status("acestep", state="running",
                    message="ACE-Step already running", port=ACESTEP_PORT)
    else:
        _set_status("acestep", state="not_running",
                    message="ACE-Step will start when music generation is needed")

    # Start the health watchdog
    start_watchdog()

    log.info("Service startup initiated (background)")


def _kill_proc(proc: subprocess.Popen | None, label: str) -> bool:
    """Terminate a managed process. Kill forcefully if it doesn't exit in 5s."""
    if proc is None or proc.poll() is not None:
        return False
    log.info("Stopping %s (pid %d)...", label, proc.pid)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log.warning("%s did not exit gracefully -- killing", label)
        proc.kill()
        proc.wait(timeout=3)
    return True


def _kill_by_port(port: int, label: str):
    """Kill any process occupying a port (for frozen processes we didn't spawn)."""
    if sys.platform != "win32":
        return
    try:
        result = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                pid = int(parts[-1])
                if pid > 0:
                    log.info("Killing frozen %s on port %d (pid %d)", label, port, pid)
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True, timeout=5)
    except Exception as e:
        log.warning("Failed to kill process on port %d: %s", port, e)


def stop_service(name: str) -> tuple[bool, str | None]:
    """Stop a single managed service."""
    global _wangp_worker_proc, _acestep_proc

    if name == "wangp":
        killed = _kill_proc(_wangp_worker_proc, "WanGP")
        if not killed:
            _kill_by_port(WANGP_WORKER_PORT, "WanGP")
        _wangp_worker_proc = None
        _set_status("wangp", state="not_running", message="WanGP stopped", pid=None)
        return True, None

    if name == "acestep":
        killed = _kill_proc(_acestep_proc, "ACE-Step")
        if not killed:
            _kill_by_port(ACESTEP_PORT, "ACE-Step")
        _acestep_proc = None
        _set_status("acestep", state="not_running", message="ACE-Step stopped", pid=None)
        return True, None

    if name == "forge":
        _kill_by_port(7861, "Forge")
        time.sleep(1)
        from services.forge_client import forge_alive
        if forge_alive():
            _set_status("forge", state="running", message="Forge still running (external)")
            return False, "Could not kill Forge -- it may be running externally"
        _set_status("forge", state="not_running", message="Forge stopped")
        return True, None

    if name == "ollama":
        _kill_by_port(11434, "Ollama")
        time.sleep(1)
        _set_status("ollama", state="not_running", message="Ollama stopped")
        return True, None

    return False, f"Unknown service: {name}"


def restart_service(name: str) -> tuple[bool, str | None]:
    """Stop then start a service."""
    stop_service(name)
    time.sleep(2)  # let ports release

    if name == "wangp":
        return start_wangp_worker()
    if name == "acestep":
        return start_acestep()
    if name == "forge":
        return start_forge()
    if name == "ollama":
        return start_ollama()
    return False, f"Unknown service: {name}"


# ── Forge auto-start ─────────────────────────────────────────────────────────

_forge_proc: subprocess.Popen | None = None

def start_forge() -> tuple[bool, str | None]:
    """Start Forge SD WebUI with --api flag."""
    global _forge_proc
    from services.forge_client import forge_alive, FORGE_PORT

    if forge_alive():
        _set_status("forge", state="running",
                    message="Forge already running", port=FORGE_PORT)
        return True, None

    forge_root = cfg.get("forge_root") or r"C:\forge"
    webui_bat = Path(forge_root) / "webui-user.bat"
    if not webui_bat.exists():
        webui_bat = Path(forge_root) / "webui.bat"
    if not webui_bat.exists():
        msg = f"Forge launch script not found in {forge_root}"
        _set_status("forge", state="error", message=msg)
        return False, msg

    _set_status("forge", state="starting",
                message="Starting Forge SD (~60s to load)...")
    log.info("Starting Forge SD from %s...", forge_root)

    env = os.environ.copy()
    env["COMMANDLINE_ARGS"] = env.get("COMMANDLINE_ARGS", "") + " --api"

    try:
        proc = subprocess.Popen(
            ["cmd", "/c", f"cd /d {forge_root} && {webui_bat.name}"],
            cwd=str(forge_root), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            **_popen_flags(),
        )
        _forge_proc = proc

        def _drain(p):
            try:
                for line in p.stdout:
                    stripped = line.rstrip()
                    if stripped:
                        log.info("[forge] %s", stripped)
            except Exception:
                pass

        threading.Thread(target=_drain, args=(proc,), daemon=True).start()

        deadline = time.time() + 120
        while time.time() < deadline:
            if forge_alive():
                _set_status("forge", state="running",
                            message="Forge WebUI running (SD image generation ready)",
                            port=FORGE_PORT, pid=proc.pid)
                log.info("Forge started on port %d", FORGE_PORT)
                return True, None
            if proc.poll() is not None:
                msg = "Forge process exited before becoming ready"
                _set_status("forge", state="error", message=msg)
                return False, msg
            time.sleep(3)

        msg = "Forge did not start within 120s"
        _set_status("forge", state="error", message=msg)
        return False, msg

    except Exception as e:
        msg = f"Failed to start Forge: {e}"
        _set_status("forge", state="error", message=msg)
        return False, msg


# ── Ollama auto-start ────────────────────────────────────────────────────────

def _find_ollama() -> str | None:
    import shutil
    found = shutil.which("ollama") or shutil.which("ollama.exe")
    if found:
        return found
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
        r"C:\Program Files\Ollama\ollama.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def ollama_alive() -> bool:
    return check_port("127.0.0.1", 11434, timeout=2)


def start_ollama() -> tuple[bool, str | None]:
    """Ensure Ollama is running. Start it if not."""
    if ollama_alive():
        return True, None

    exe = _find_ollama()
    if not exe:
        return False, "Ollama executable not found"

    log.info("Starting Ollama serve...")
    try:
        subprocess.Popen(
            [exe, "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **_popen_flags(),
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            if ollama_alive():
                log.info("Ollama is ready on port 11434")
                return True, None
            time.sleep(1)
        return False, "Ollama did not start within 15s"
    except Exception as e:
        return False, f"Failed to start Ollama: {e}"


# ── Health watchdog ──────────────────────────────────────────────────────────

def _watchdog_loop():
    """Periodically check managed services and restart crashed ones."""
    while True:
        time.sleep(30)
        try:
            status = get_status()

            # Check WanGP worker -- restart if we launched it but it died
            if _wangp_worker_proc and _wangp_worker_proc.poll() is not None:
                log.warning("[watchdog] WanGP worker died -- restarting")
                _set_status("wangp", state="error", message="Worker crashed -- restarting...")
                start_wangp_worker()

            # Check ACE-Step
            if _acestep_proc and _acestep_proc.poll() is not None:
                log.warning("[watchdog] ACE-Step died -- restarting")
                _set_status("acestep", state="error", message="Server crashed -- restarting...")
                start_acestep()

            # Check Forge -- if we started it and it died
            if _forge_proc and _forge_proc.poll() is not None:
                log.warning("[watchdog] Forge died -- restarting")
                _set_status("forge", state="error", message="Forge crashed -- restarting...")
                start_forge()

            # Ensure Ollama stays up (lightweight check)
            if not ollama_alive():
                start_ollama()

        except Exception as e:
            log.debug("[watchdog] Error: %s", e)


def start_watchdog():
    """Launch the health watchdog in a background thread."""
    threading.Thread(target=_watchdog_loop, daemon=True, name="svc-watchdog").start()
    log.info("Service health watchdog started (30s interval)")


# ── Lifecycle ────────────────────────────────────────────────────────────────

def shutdown_all():
    """Cleanly shut down managed services."""
    global _wangp_worker_proc, _acestep_proc, _forge_proc
    for label, proc in [("WanGP", _wangp_worker_proc), ("ACE-Step", _acestep_proc),
                        ("Forge", _forge_proc)]:
        _kill_proc(proc, label)
    _wangp_worker_proc = None
    _acestep_proc = None
    _forge_proc = None
