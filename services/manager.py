"""Service detection and auto-launch for WanGP and ACE-Step.

Adapted from DropCatGo-Fun-Videos_w_Audio/services.py. Manages external
AI services that multiple features depend on.
"""
from __future__ import annotations

import ctypes
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

# -- Windows Job Object -- kill GPU children when DCS dies for any reason -------
# A Job Object with KILL_ON_JOB_CLOSE is the OS-level guarantee: when the DCS
# Python process exits (clean, crash, or Task Manager kill), Windows closes the
# job handle and immediately terminates every process assigned to it.
# This prevents WanGP / ACE-Step from surviving as orphan GPU hogs.

_JOB_HANDLE: ctypes.c_void_p | None = None

def _init_job_object() -> None:
    global _JOB_HANDLE
    if sys.platform != "win32" or _JOB_HANDLE is not None:
        return
    try:
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        JobObjectExtendedLimitInformation   = 9

        class _BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit",     ctypes.c_int64),
                ("LimitFlags",             ctypes.c_uint32),
                ("MinimumWorkingSetSize",   ctypes.c_size_t),
                ("MaximumWorkingSetSize",   ctypes.c_size_t),
                ("ActiveProcessLimit",      ctypes.c_uint32),
                ("Affinity",               ctypes.c_size_t),
                ("PriorityClass",           ctypes.c_uint32),
                ("SchedulingClass",         ctypes.c_uint32),
            ]

        class _IO(ctypes.Structure):
            _fields_ = [(f, ctypes.c_uint64) for f in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount",  "WriteTransferCount",  "OtherTransferCount",
            )]

        class _EXT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC),
                ("IoInfo",                _IO),
                ("ProcessMemoryLimit",    ctypes.c_size_t),
                ("JobMemoryLimit",        ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed",     ctypes.c_size_t),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        job = k32.CreateJobObjectW(None, None)
        if not job:
            log.warning("Job Object: CreateJobObjectW failed (%s)", ctypes.get_last_error())
            return

        info = _EXT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = k32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        )
        if not ok:
            log.warning("Job Object: SetInformationJobObject failed (%s)", ctypes.get_last_error())
            k32.CloseHandle(job)
            return

        _JOB_HANDLE = job
        log.info("Job Object armed -- GPU subprocesses will die with DCS (any exit)")

        # Register atexit to explicitly close the handle so KILL_ON_JOB_CLOSE fires.
        # Python does not track ctypes handles -- without this the Job Object handle
        # leaks on process exit and the kill never triggers.
        import atexit
        def _close_job():
            try:
                ctypes.WinDLL("kernel32").CloseHandle(job)
            except Exception:
                pass
        atexit.register(_close_job)

    except Exception as exc:
        log.warning("Job Object setup failed (non-fatal): %s", exc)


def _assign_to_job(proc: subprocess.Popen) -> None:
    """Assign a subprocess to the DCS Job Object so it dies when DCS dies."""
    if _JOB_HANDLE is None or sys.platform != "win32":
        return
    try:
        PROCESS_ALL_ACCESS = 0x001F0FFF
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(PROCESS_ALL_ACCESS, False, proc.pid)
        if handle:
            ok = k32.AssignProcessToJobObject(_JOB_HANDLE, handle)
            k32.CloseHandle(handle)
            if not ok:
                # Fails if the process is already in a Job Object that forbids
                # nesting (e.g. Pinokio's Job Object on Windows 7). On Windows 8+
                # nested jobs are supported; this error means something else.
                # shutdown_all()._kill_stale_gpu_processes() is the backstop.
                log.warning(
                    "Job Object: could not assign PID %d (error %d) -- "
                    "process will be killed by name scan on shutdown",
                    proc.pid, ctypes.get_last_error(),
                )
    except Exception as exc:
        log.debug("Could not assign PID %s to Job Object: %s", proc.pid, exc)


_init_job_object()


def _kill_stale_gpu_processes() -> None:
    """Kill orphan WanGP / ACE-Step processes left over from a previous DCS session.

    When DCS restarts but _assign_to_job() failed (e.g. because Pinokio puts its
    Python process in its own Job Object), GPU workers survive as orphans that
    consume VRAM without serving requests.  We scan by command-line pattern and
    kill any matches before starting fresh workers.
    """
    if sys.platform != "win32":
        return
    own_pid = os.getpid()
    patterns = [
        ("wangp_worker.py", "WanGP worker"),
        ("api_server.py", "ACE-Step"),
    ]
    for pattern, label in patterns:
        try:
            ps_cmd = (
                "Get-WmiObject Win32_Process | "
                f"Where-Object {{ $_.CommandLine -like '*{pattern}*' }} | "
                "Select-Object ProcessId | "
                "ForEach-Object { $_.ProcessId }"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=15, **_popen_flags(),
            )
            for line in result.stdout.splitlines():
                pid_str = line.strip()
                if not pid_str.isdigit():
                    continue
                pid = int(pid_str)
                if pid == 0 or pid == own_pid:
                    continue
                log.info("Killing stale %s orphan (PID %d)", label, pid)
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, timeout=5, **_popen_flags(),
                )
        except Exception as e:
            log.debug("Could not scan for stale %s processes: %s", label, e)


def _popen_flags() -> dict:
    """Return creationflags to hide console windows on Windows (unless debug mode)."""
    if sys.platform == "win32" and not cfg.get("debug_mode"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}

# -- Status tracking ----------------------------------------------------------

_status_lock = threading.Lock()
_service_status: dict = {
    "wangp":   {"state": "unknown", "message": "", "port": None, "pid": None},
    "acestep": {"state": "unknown", "message": "", "port": None, "pid": None},
    "forge":   {"state": "unknown", "message": "", "port": 7861, "pid": None},
    "ollama":  {"state": "unknown", "message": "", "port": 11434, "pid": None},
}

_wangp_worker_proc: subprocess.Popen | None = None
_acestep_proc: subprocess.Popen | None = None
_watchdog_stop = threading.Event()   # set by shutdown_all() to halt the watchdog

# BUG-11: guard against two concurrent calls both passing the alive-check and
# starting duplicate worker processes.
_wangp_start_lock = threading.Lock()
_acestep_start_lock = threading.Lock()


def get_status() -> dict:
    with _status_lock:
        return {k: dict(v) for k, v in _service_status.items()}


def _set_status(service: str, **kwargs):
    with _status_lock:
        _service_status[service].update(kwargs)


# -- Port / HTTP helpers ------------------------------------------------------

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


# -- ACE-Step -----------------------------------------------------------------

ACESTEP_PORT = 8020


def _acestep_host() -> str:
    """Return the configured ACE-Step host (local or remote IP)."""
    try:
        return (cfg.get("acestep_host") or "localhost").strip()
    except Exception:
        return "localhost"


def _is_remote_host(host_or_url: str) -> bool:
    h = host_or_url.lower().replace("http://", "").replace("https://", "").split(":")[0].split("/")[0]
    return bool(h) and h not in ("localhost", "127.0.0.1")


def acestep_alive() -> bool:
    return http_get(f"http://{_acestep_host()}:{ACESTEP_PORT}/health", timeout=3) is not None


def start_acestep() -> tuple[bool, str | None]:
    """Start ACE-Step API server headlessly if not already running.

    When acestep_host points to a remote machine, skips local subprocess
    launch and just checks the remote endpoint is reachable.
    Supports both venv-based (.venv/Scripts/python.exe) and uv-based installs.
    """
    import shutil
    global _acestep_proc

    host = _acestep_host()

    if _is_remote_host(host):
        if acestep_alive():
            _set_status("acestep", state="running",
                        message=f"ACE-Step (remote) running at {host}:{ACESTEP_PORT}", port=ACESTEP_PORT)
            return True, None
        msg = f"ACE-Step not reachable at {host}:{ACESTEP_PORT} -- is it running on the remote machine?"
        _set_status("acestep", state="not_running", message=msg)
        return False, msg

    with _acestep_start_lock:
        if acestep_alive():
            _set_status("acestep", state="running",
                        message="ACE-Step server already running", port=ACESTEP_PORT)
            log.info("ACE-Step already running on port %d", ACESTEP_PORT)
            return True, None

        acestep_root = cfg.get_acestep_root()
        if acestep_root is None:
            msg = "ACE-Step path not configured -- set it in Settings"
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
            cmd = [str(python), str(api_script), "--host", host, "--port", str(ACESTEP_PORT)]
        elif uv_exe:
            cmd = [uv_exe, "run", "--no-sync", "acestep-api",
                   "--host", host, "--port", str(ACESTEP_PORT)]
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
        # Force the SFT (Supervised Fine-Tuned) checkpoint instead of the
        # default turbo variant. Turbo is distilled to 8 steps and produces
        # music beds without intelligible vocals -- SFT accepts 20+ steps and
        # actually sings the lyrics we pass in. The SFT model must be
        # downloaded first: `acestep-download --model acestep-v15-sft`.
        # If SFT isn't on disk, ACE-Step falls back to whatever is available.
        #
        # CRITICAL: ACE-Step's api_server.py reads ACESTEP_CONFIG_PATH for
        # the DiT model name. The SERVICE_MODE_DIT_MODEL env var only
        # affects the acestep_v15_pipeline.py CLI entry point, NOT the API
        # server we launch. See acestep/api/startup_model_init.py:68.
        sft_path = acestep_root / "checkpoints" / "acestep-v15-sft"
        if sft_path.is_dir():
            env["ACESTEP_CONFIG_PATH"] = "acestep-v15-sft"
            log.info("ACE-Step DiT model: acestep-v15-sft (vocals-capable)")
        else:
            log.warning("ACE-Step SFT checkpoint not found at %s -- vocals will be "
                        "muted on Turbo. Download: acestep-download --model acestep-v15-sft",
                        sft_path)
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(acestep_root) + (os.pathsep + existing_pp if existing_pp else "")

        try:
            proc = subprocess.Popen(
                cmd, cwd=str(acestep_root), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                **_popen_flags(),
            )
            _assign_to_job(proc)
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

            deadline = time.time() + 300  # 5 min -- LM model loading takes ~90s

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
            # uvicorn queues HTTP requests during startup -- issue one long-timeout
            # request so we get the response as soon as initialization finishes.
            log.info("ACE-Step port %d open -- waiting for model to load...", ACESTEP_PORT)
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
                                message="ACE-Step ready -- music generation available",
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


# -- WanGP --------------------------------------------------------------------

WANGP_GRADIO_PORTS = [7862, 7863, 7864]  # 7860=DropCat, 7861=Forge
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
    return {"state": "ready", "message": "WanGP configured -- will use subprocess per request", "mode": "subprocess"}


def start_wangp_worker() -> tuple[bool, str | None]:
    """Start the persistent WanGP worker process.

    BUG-11: _wangp_start_lock serializes concurrent calls so only one
    subprocess is ever launched (two rapid UI clicks could otherwise both
    pass the alive check and each start a worker, leaking the first).
    """
    global _wangp_worker_proc

    with _wangp_start_lock:
        # Always evict orphan workers before deciding whether one is running.
        # Without this, a stale worker from a previous DCS session can pass the
        # health check, trigger an early return, and then silently coexist with
        # a second worker spawned later -- two workers, double VRAM, GPU fan noise.
        _kill_stale_gpu_processes()

        if wangp_worker_alive():
            _set_status("wangp", state="running", message="WanGP worker already running")
            return True, None

        wan_root = cfg.get("wan2gp_root")
        if not wan_root:
            msg = "WanGP path not configured -- set it in Settings"
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

        # Kill any lingering worker processes before starting a new one so we
        # don't leak orphan GPU processes across restarts. Use both strategies:
        # port kill catches anything holding 7899 (including Pinokio-spawned
        # miniconda workers that survive the wmic process-name scan).
        _kill_by_port(WANGP_WORKER_PORT, "WanGP", wait_release=True)
        _kill_stale_gpu_processes()

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
            _assign_to_job(proc)
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


# -- Lifecycle ----------------------------------------------------------------

def quick_detect():
    """Fast synchronous snapshot of which services are already running.

    Called in the lifespan before the server starts accepting requests so the
    very first /api/system response returns real states instead of 'unknown'.
    Each check is just a socket connect -- completes in <50ms if the service
    is up, or fails fast (connection refused) if it's down.
    """
    if wangp_worker_alive():
        _set_status("wangp", state="running",
                    message="WanGP worker running (model loaded)",
                    port=WANGP_WORKER_PORT)
    else:
        wan_root = cfg.get("wan2gp_root")
        if wan_root:
            _set_status("wangp", state="ready",
                        message="WanGP configured -- starting worker...")
        else:
            _set_status("wangp", state="not_configured",
                        message="WanGP path not configured")

    try:
        from services.forge_client import forge_alive as _fa, FORGE_PORT
        if _fa():
            _set_status("forge", state="running",
                        message="Forge API running (SD image generation ready)",
                        port=FORGE_PORT)
        else:
            _set_status("forge", state="not_running",
                        message="Forge not running -- use Services tab to start")
    except Exception:
        _set_status("forge", state="not_running", message="Forge not detected")

    if ollama_alive():
        _set_status("ollama", state="running",
                    message="Ollama running on port 11434", port=11434)
    else:
        _set_status("ollama", state="not_running", message="Ollama not detected")

    if acestep_alive():
        _set_status("acestep", state="running",
                    message="ACE-Step already running", port=ACESTEP_PORT)
    else:
        acestep_root = cfg.get("acestep_root")
        if acestep_root:
            _set_status("acestep", state="ready",
                        message="ACE-Step ready -- starts automatically when you generate music")
        else:
            _set_status("acestep", state="not_configured",
                        message="ACE-Step not configured -- set path in Settings")

    log.info("Quick service detect complete")


def startup_all():
    """Detect and start services concurrently on app startup.

    Orphan eviction has already run synchronously via kill_orphans_at_startup()
    in the FastAPI lifespan, so we don't repeat the WMIC scan here.
    """
    quick_detect()
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
                log.info("ACE-Step auto-detected at %s -- saving to config", detected)
                cfg.save({"acestep_root": detected})
        start_acestep()

    def _start_wan():
        wangp_status = check_wangp()
        _set_status("wangp", **wangp_status)
        wan_root = cfg.get("wan2gp_root")
        if wan_root and wangp_status["state"] == "ready":
            ok, _ = start_wangp_worker()
            if ok:
                # Tell the GPU orchestrator WanGP owns the GPU so the first
                # multi-clip job sees it as warm and skips Phase 0 audio-first.
                # Without this, gpu.current stays None despite WanGP being loaded,
                # and Phase 0 evicts the freshly-loaded worker on every first job.
                try:
                    from core.gpu_orchestrator import gpu as _gpu
                    _gpu._current = "wangp"
                except Exception:
                    pass
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

    def _ensure_ollama():
        ollama_url = cfg.get("ollama_host") or "http://localhost:11434"
        if _is_remote_host(ollama_url.replace("http://", "").split(":")[0]):
            # Remote Ollama -- just verify it's reachable
            import urllib.request
            try:
                urllib.request.urlopen(ollama_url.rstrip("/") + "/api/tags", timeout=3)
                _set_status("ollama", state="running",
                            message=f"Ollama (remote) running at {ollama_url}", port=11434)
            except Exception:
                _set_status("ollama", state="not_running",
                            message=f"Ollama not reachable at {ollama_url}")
            return
        ok, err = start_ollama()
        if ok:
            _set_status("ollama", state="running",
                        message="Ollama running on port 11434", port=11434)
        else:
            _set_status("ollama", state="not_running",
                        message=err or "Ollama not available")

    # Ollama is backup-only: do NOT auto-start the local process unless the user
    # has explicitly opted in (provider == "ollama", or fallback enabled). A remote
    # Ollama host is always just health-checked (_ensure_ollama never spawns it).
    # When opted out, leave it stopped; start it on demand from the Services panel.
    _ollama_host = cfg.get("ollama_host") or "http://localhost:11434"
    _ollama_remote = _is_remote_host(_ollama_host.replace("http://", "").split(":")[0])
    _ollama_optin = (cfg.get("llm_provider") == "ollama") or bool(cfg.get("allow_ollama_fallback"))

    _start_list = [("WanGP", _start_wan)]
    if _ollama_remote or _ollama_optin:
        _start_list.append(("Ollama", _ensure_ollama))
    else:
        _set_status("ollama", state="not_running",
                    message="Ollama is backup-only (not auto-started); start it from Services when needed")
    for label, fn in _start_list:
        threading.Thread(target=_safe_run, args=(label, fn), daemon=True).start()

    # ACE-Step: deferred -- only started when music generation is needed.
    # Keeps VRAM free for Ollama vision models during prompt generation.
    if _is_remote_host(_acestep_host()):
        if acestep_alive():
            _set_status("acestep", state="running",
                        message=f"ACE-Step (remote) running at {_acestep_host()}:{ACESTEP_PORT}", port=ACESTEP_PORT)
        else:
            _set_status("acestep", state="not_running",
                        message=f"ACE-Step not reachable at {_acestep_host()}:{ACESTEP_PORT}")
    elif acestep_alive():
        _set_status("acestep", state="running",
                    message="ACE-Step already running", port=ACESTEP_PORT)
    else:
        acestep_root = cfg.get("acestep_root")
        if acestep_root:
            _set_status("acestep", state="ready",
                        message="ACE-Step ready -- starts automatically when you generate music")
        else:
            _set_status("acestep", state="not_configured",
                        message="ACE-Step not configured -- set path in Settings for music generation")

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


def _kill_by_port(port: int, label: str, wait_release: bool = False) -> bool:
    """Kill any process occupying a port (for frozen processes we didn't spawn).

    Uses /F /T so child processes (CUDA workers, etc.) are killed too. When
    wait_release is True, polls the port for up to 4s to confirm it's free
    before returning -- needed at startup so the new worker can bind cleanly.
    Returns True if a kill was attempted.
    """
    if sys.platform != "win32":
        return False
    killed_any = False
    try:
        result = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=5, **_popen_flags(),
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                pid = int(parts[-1])
                if pid > 0:
                    log.info("Killing frozen %s on port %d (pid %d)", label, port, pid)
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   capture_output=True, timeout=5, **_popen_flags())
                    killed_any = True
    except Exception as e:
        log.warning("Failed to kill process on port %d: %s", port, e)

    if killed_any and wait_release:
        # Poll until the port no longer appears as LISTENING -- max 4s.
        deadline = time.time() + 4.0
        while time.time() < deadline:
            try:
                result = subprocess.run(
                    ["netstat", "-ano"], capture_output=True, text=True, timeout=2, **_popen_flags(),
                )
                still_listening = any(
                    f":{port}" in ln and "LISTENING" in ln
                    for ln in result.stdout.splitlines()
                )
                if not still_listening:
                    log.info("Port %d released after kill", port)
                    return True
            except Exception:
                break
            time.sleep(0.3)
        log.warning("Port %d still LISTENING after 4s -- new %s may fail to bind", port, label)
    return killed_any


def kill_orphans_at_startup() -> None:
    """Synchronously evict orphan WanGP / ACE-Step workers on app startup.

    Runs BEFORE FastAPI starts accepting requests so a previous DCS session's
    GPU subprocesses (which the OS has not yet finished tearing down) can't
    keep VRAM hostage and force the user to wait for the old job to complete.

    Fast path: netstat + taskkill /F /T on ports 7899 and 8020 (~1s).
    Backstop: WMIC command-line scan for orphans on different ports (~10s).
    """
    log.info("Evicting any orphan GPU workers from prior session...")
    _kill_by_port(WANGP_WORKER_PORT, "WanGP", wait_release=True)
    _kill_by_port(ACESTEP_PORT, "ACE-Step", wait_release=True)
    _kill_stale_gpu_processes()


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
        # After stopping, go back to "ready" if the path is configured so the UI
        # shows "starts automatically" instead of the misleading "set path in Settings".
        if cfg.get_acestep_root():
            _set_status("acestep", state="ready",
                        message="ACE-Step stopped -- will restart automatically when music is needed", pid=None)
        else:
            _set_status("acestep", state="not_configured",
                        message="ACE-Step stopped -- set path in Settings", pid=None)
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


# -- Forge auto-start ---------------------------------------------------------

_forge_proc: subprocess.Popen | None = None

def _forge_port_open(port: int = 7861) -> bool:
    """True if something is already listening on the Forge port (even mid-startup)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except Exception:
        return False


def start_forge() -> tuple[bool, str | None]:
    """Start Forge SD WebUI as a fully detached process.

    Forge is launched via PowerShell Start-Process so it runs independently
    of Drop Cat Go Studio -- it survives app restarts and closing the app.

    If Forge's port is already open (model still loading), we skip the launch
    and just wait for the API to become ready -- prevents killing a loading Forge.
    """
    global _forge_proc
    from services.forge_client import forge_alive, FORGE_PORT

    if forge_alive():
        _set_status("forge", state="running",
                    message="Forge API running (SD image generation ready)",
                    port=FORGE_PORT)
        _forge_proc = None
        return True, None

    # Port open but API not yet ready = Forge is mid-startup; don't kill it
    if _forge_port_open(FORGE_PORT):
        log.info("Forge port %d is open -- model still loading, waiting...", FORGE_PORT)
        _set_status("forge", state="starting",
                    message="Forge loading model, please wait (~90s)...")
        # Fall through to the poll loop below without launching a new process
        forge_root = cfg.get("forge_root") or r"C:\forge"
        webui_bat = None   # signal: skip the launch step
    else:
        forge_root = cfg.get("forge_root") or r"C:\forge"
        webui_bat = Path(forge_root) / "webui-user.bat"
        if not webui_bat.exists():
            webui_bat = Path(forge_root) / "webui.bat"
        if not webui_bat.exists():
            msg = f"Forge launch script not found in {forge_root}"
            _set_status("forge", state="error", message=msg)
            return False, msg

    try:
        if webui_bat is not None:
            # Fresh launch -- Forge isn't running at all
            _set_status("forge", state="starting",
                        message="Starting Forge SD -- loading model, please wait (~90s)...")
            log.info("Starting Forge SD from %s (detached)...", forge_root)

            # IMPORTANT (2026-06-18): launch Forge's entry point DIRECTLY with its
            # venv Python -- do NOT run webui-user.bat. That .bat frees port 7861 via
            #   netstat -ano | findstr ":7861 "  ->  taskkill /F /PID <col 5>
            # which also matches THIS app's own outbound polling connections to 7861
            # (the PID in those rows is the client side = us), so running it would
            # taskkill the Drop Cat Go Studio server itself -- the "exit code 1" crash.
            # Start-Process still detaches Forge so it survives app restarts.
            # --nowebui runs Forge as a HEADLESS API ONLY (no Gradio web UI) -- DCS
            # only ever talks to /sdapi/v1, so there is no reason to serve (or pop
            # up) the Stable Diffusion GUI, and a headless server can't be closed
            # by accident. WEBUI_LAUNCH_LIVE_PREVIEW=0 also stops any browser open.
            forge_py = Path(forge_root) / "venv" / "Scripts" / "python.exe"
            entry = "launch.py" if (Path(forge_root) / "launch.py").exists() else "webui.py"
            if forge_py.exists():
                target = str(forge_py)
                arglist = f"'{entry}','--cuda-malloc','--api','--nowebui','--port','{FORGE_PORT}'"
            else:
                # venv Python missing -- last-resort fallback to the .bat
                target = "cmd.exe"
                arglist = f"'/c','\"{webui_bat}\"'"
            ps_args = (
                f"$env:WEBUI_LAUNCH_LIVE_PREVIEW='0'; "
                f"Start-Process -FilePath '{target}'"
                f" -ArgumentList {arglist}"
                f" -WorkingDirectory '{forge_root}'"
                f" -WindowStyle Hidden"
            )
            subprocess.Popen(
                ["powershell", "-WindowStyle", "Hidden", "-NonInteractive",
                 "-Command", ps_args],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            log.info("Forge launch command sent -- polling for API...")
        _forge_proc = None  # detached -- no handle to track

        # Poll until Forge API responds (up to 5 minutes for large models)
        deadline = time.time() + 300
        last_log = time.time()
        while time.time() < deadline:
            if forge_alive():
                _set_status("forge", state="running",
                            message="Forge API running (SD image generation ready)",
                            port=FORGE_PORT)
                log.info("Forge ready on port %d", FORGE_PORT)
                return True, None
            if time.time() - last_log >= 15:
                elapsed = int(time.time() - (deadline - 300))
                _set_status("forge", state="starting",
                            message=f"Forge loading... ({elapsed}s elapsed, up to 5min for large models)")
                log.info("Forge still loading... (%ds elapsed)", elapsed)
                last_log = time.time()
            time.sleep(5)

        msg = "Forge did not respond within 5 minutes -- check C:\\forge\\webui-user.bat"
        _set_status("forge", state="error", message=msg)
        log.error(msg)
        return False, msg

    except Exception as e:
        msg = f"Failed to launch Forge: {e}"
        _set_status("forge", state="error", message=msg)
        log.error(msg)
        return False, msg


# -- Ollama auto-start --------------------------------------------------------

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


# -- Health watchdog ----------------------------------------------------------

# Stuck-WanGP detection: track the last time the step counter changed.
# If busy=True but the step hasn't advanced in _WANGP_STUCK_SECS, the
# generation is deadlocked and we restart. 300s (5 min) is very generous --
# even the slowest model step (LTX-2 19B step 0) takes ~14s.
_WANGP_STUCK_SECS = 120  # 2 min -- faster zombie detection = less RAM held by stuck process
_wangp_last_step: int | None = None
_wangp_last_step_time: float = 0.0

def _watchdog_loop():
    """Periodically check managed services and restart crashed ones."""
    global _wangp_last_step, _wangp_last_step_time
    while not _watchdog_stop.is_set():
        # Interruptible sleep -- wakes immediately when shutdown_all() fires
        _watchdog_stop.wait(30)
        if _watchdog_stop.is_set():
            log.debug("[watchdog] stop event received -- exiting")
            return
        try:
            status = get_status()

            # Check WanGP worker -- restart if we launched it but it died.
            # Snapshot the global to a local so a concurrent stop_service()
            # setting it to None can't NPE the .poll() call.
            #
            # IMPORTANT: route the respawn through the GPU orchestrator so
            # the eviction policy fires. Without this, the watchdog would
            # call start_wangp_worker() directly while ACE-Step or Forge is
            # already holding 5-7 GB of VRAM, and the two services end up
            # co-loaded on a 16 GB card (observed 2026-05-11: VRAM thrash
            # at 97% util, machine freeze). gpu.acquire("wangp") evicts
            # whatever else currently holds the GPU before starting.
            wangp_proc = _wangp_worker_proc
            if wangp_proc and wangp_proc.poll() is not None:
                log.warning("[watchdog] WanGP worker died -- restarting via orchestrator")
                _set_status("wangp", state="error", message="Worker crashed -- restarting...")
                _wangp_last_step = None  # reset stuck tracker on respawn
                try:
                    from core.gpu_orchestrator import gpu
                    gpu.acquire("wangp", reason="watchdog respawn after crash")
                except Exception as e:
                    log.error("[watchdog] orchestrator-aware WanGP respawn failed: %s; "
                              "falling back to direct start", e)
                    start_wangp_worker()
            elif wangp_proc and wangp_proc.poll() is None:
                # Process is alive -- check for stuck generation (busy=True but
                # step hasn't advanced for _WANGP_STUCK_SECS).
                try:
                    with urllib.request.urlopen(
                        "http://127.0.0.1:7899/status"
                    ) as _r:
                        _ws = json.loads(_r.read())
                    if _ws.get("busy"):
                        _cur_step = _ws.get("step", 0)
                        if _wangp_last_step != _cur_step:
                            _wangp_last_step = _cur_step
                            _wangp_last_step_time = time.time()
                        elif time.time() - _wangp_last_step_time > _WANGP_STUCK_SECS:
                            log.warning(
                                "[watchdog] WanGP busy but step stuck at %d for >%ds"
                                " -- treating as deadlocked, restarting",
                                _cur_step, _WANGP_STUCK_SECS,
                            )
                            _set_status("wangp", state="error",
                                        message="Generation deadlocked -- restarting...")
                            _wangp_last_step = None
                            # Kill by port first -- the tracked _wangp_worker_proc
                            # reference may point to a newer process started by a
                            # previous recovery attempt, leaving the original stuck
                            # process still alive and holding VRAM. Port-kill catches
                            # ALL processes on 7899 regardless of which one we spawned.
                            _kill_by_port(WANGP_WORKER_PORT, "WanGP (deadlock)", wait_release=True)
                            try:
                                from core.gpu_orchestrator import gpu
                                gpu.acquire("wangp", reason="watchdog deadlock recovery")
                            except Exception as _e:
                                log.error("[watchdog] deadlock recovery failed: %s", _e)
                                start_wangp_worker()
                    else:
                        # Not busy -- reset tracker so next busy phase starts fresh
                        _wangp_last_step = None
                        _wangp_last_step_time = 0.0
                except Exception:
                    pass  # WanGP temporarily unreachable -- not fatal

            # Check ACE-Step (same snapshot pattern as WanGP).
            # Same orchestrator-aware respawn rationale as WanGP above.
            acestep_proc = _acestep_proc
            if acestep_proc and acestep_proc.poll() is not None:
                log.warning("[watchdog] ACE-Step died -- restarting via orchestrator")
                _set_status("acestep", state="error", message="Server crashed -- restarting...")
                try:
                    from core.gpu_orchestrator import gpu
                    gpu.acquire("acestep", reason="watchdog respawn after crash")
                except Exception as e:
                    log.error("[watchdog] orchestrator-aware ACE-Step respawn failed: %s; "
                              "falling back to direct start", e)
                    start_acestep()

            # Forge is user-managed (image gen is separate from video gen).
            # Just passively detect its state -- never auto-restart it.
            from services.forge_client import forge_alive as _forge_alive
            forge_state = status.get("forge", {}).get("state", "unknown")
            if forge_state in ("not_running", "unknown", "error", "ready"):
                if _forge_alive():
                    _set_status("forge", state="running",
                                message="Forge API running (SD image generation ready)",
                                port=7861)
            elif forge_state == "running":
                if not _forge_alive():
                    _set_status("forge", state="not_running",
                                message="Forge not running -- use Services tab to start")

            # Ensure Ollama stays up (lightweight check)
            if not ollama_alive():
                start_ollama()

        except Exception as e:
            log.debug("[watchdog] Error: %s", e)


def start_watchdog():
    """Launch the health watchdog in a background thread."""
    threading.Thread(target=_watchdog_loop, daemon=True, name="svc-watchdog").start()
    log.info("Service health watchdog started (30s interval)")


# -- Lifecycle ----------------------------------------------------------------

def shutdown_all():
    """Cleanly shut down managed services."""
    global _wangp_worker_proc, _acestep_proc, _forge_proc

    # Stop the watchdog first so it cannot respawn a service we are about to
    # kill. Without this there is a race: the watchdog wakes, sees a dead proc,
    # and calls gpu.acquire() to restart it while shutdown_all() is mid-kill.
    _watchdog_stop.set()

    # Ask WanGP worker to flush CUDA memory before we force-kill it.
    # The /shutdown endpoint runs torch.cuda.empty_cache/synchronize so the
    # GPU driver can reclaim VRAM cleanly -- skipping this causes the CUDA
    # context to die mid-flight and the display driver resets the GPU, which
    # makes the monitor go dark for 1-2 seconds.
    #
    # Sequence:
    #   1. POST /abort  -- sets abort flag; generation stops at next tqdm step
    #                      (up to ~14s on profile 3). Without this, synchronize()
    #                      in the /shutdown handler blocks until the step finishes.
    #   2. POST /shutdown -- flushes CUDA then calls os._exit(0)
    #   3. Poll proc.poll() up to 20s -- the process dies on its own once the
    #      flush completes; only force-kill if it hasn't exited by then.
    try:
        import urllib.request as _ur
        try:
            _ur.urlopen(f"http://127.0.0.1:{WANGP_WORKER_PORT}/abort",
                        data=b'', timeout=2)
            log.info("[shutdown] WanGP abort signal sent")
        except Exception:
            pass   # not busy or not running -- continue to /shutdown
        _ur.urlopen(f"http://127.0.0.1:{WANGP_WORKER_PORT}/shutdown", timeout=3)
        log.info("[shutdown] WanGP graceful shutdown requested -- waiting for CUDA flush")
        # Poll until the process exits or 20s elapses (covers worst-case 14s step)
        proc = _wangp_worker_proc
        deadline = time.time() + 20
        while proc is not None and proc.poll() is None and time.time() < deadline:
            time.sleep(0.5)
        if proc is not None and proc.poll() is None:
            log.warning("[shutdown] WanGP still alive after 20s -- force-killing")
        else:
            log.info("[shutdown] WanGP exited cleanly")
    except Exception:
        pass   # worker not running or already dead -- fall through to force-kill

    for label, proc in [("WanGP", _wangp_worker_proc), ("ACE-Step", _acestep_proc),
                        ("Forge", _forge_proc)]:
        _kill_proc(proc, label)
    # Belt+suspenders: kill by port for processes we didn't spawn (or lost the handle to)
    _kill_by_port(WANGP_WORKER_PORT, "WanGP")
    _kill_by_port(ACESTEP_PORT, "ACE-Step")
    _kill_stale_gpu_processes()
    _wangp_worker_proc = None
    _acestep_proc = None
    _forge_proc = None
